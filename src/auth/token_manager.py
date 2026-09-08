"""
token_manager — authentication + REST clients, per connectivity mode (master §1a, Plan 3 §2).

The workspace a notebook RUNS IN is always reached with the run-as workspace-admin SP's
**notebook-context token** (SDK ambient auth, context-token fallback). What changes by mode is
only *how the SOURCE workspace is reached*:

  • `airgap` — it isn't. Each side calls only its own workspace; the file bundle is the only thing
    that crosses. `build_clients()` returns the same local client twice.
  • `direct` — every stage runs in the TARGET, and inventory/export reach the SOURCE over REST with
    an **OAuth M2M (client-credentials)** token for a source workspace-admin SP
    (`oauth_m2m_token_provider`). `build_clients()` returns (source_client, target_client).

  • **No PATs, ever** (disabled in the customer's workspace).
  • The M2M secret is read at runtime from a target secret scope (preferred) or a widget, and is
    never logged, never persisted (`Config.redacted()` strips it).

There is deliberately NO Azure AD token minting here (IMP-4, 2026-08-08): creating an Azure Key
Vault-backed secret scope would need one, but no obtainable credential in this deployment can
produce it (a Databricks SPN secret yields a Databricks token; a managed-identity-backed SPN can
only use Azure IMDS, unreachable from a private/notebook-only workspace). AKV-backed scopes are
handled as a MANUAL step in the secrets importer instead — see the note by `AZURE_DATABRICKS_APP_ID`.

Auth strategy for the local client (adapted from the customer inventory notebook driver):
  1. SDK `WorkspaceClient()` ambient auth (works on serverless / shared / single-user) →
     read the bearer token + host from its config;
  2. fallback to the notebook-context token (classic single-user clusters).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

import requests

from src.config.config_manager import WorkspaceContext
from src.utils.logger import get_logger
from src.utils.retry import RetryableHTTPError, is_retryable_status, with_retry

_LOG = get_logger("auth")


class HTTPStatusError(Exception):
    """A non-retryable 4xx/5xx whose message CARRIES THE SERVER'S EXPLANATION.

    `requests.raise_for_status()` produces "400 Client Error: Bad Request for url: …", which tells
    an operator nothing about what to fix. Databricks always explains itself in the response body
    (`error_code` + `message`), so that text is folded into the message here — it is what makes an
    import failure actionable, and what `classify_error` matches on to recognise
    RESOURCE_ALREADY_EXISTS, PERMISSION_DENIED and friends.
    """

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"HTTP {status}")
        self.status = status


class DownloadHTTPError(Exception):
    """A non-retryable 4xx/5xx during a raw byte download; carries status + server body."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"HTTP {status}")
        self.status = status


class OversizeError(Exception):
    """A raw download was refused/aborted because it exceeded the caller's byte cap (§5a)."""

    def __init__(self, size: int, message: str = "") -> None:
        super().__init__(message or f"oversize: {size} bytes")
        self.size = size


# ---------------------------------------------------------------------------
# Context resolution (this workspace's run-as SP token + host)
# ---------------------------------------------------------------------------

def resolve_context(dbutils=None, spark=None, account_id: str = "") -> WorkspaceContext:
    """Resolve THIS workspace's host + bearer token from the run-as identity.

    Order: SDK ambient auth first (serverless/shared/single-user), then notebook context token.
    Raises RuntimeError if neither yields a token.
    """
    host, token = "", ""

    # 1. Databricks SDK ambient auth
    try:
        from databricks.sdk import WorkspaceClient
        wc = WorkspaceClient()
        auth_header = wc.config.authenticate() or {}
        bearer = auth_header.get("Authorization", "")
        if bearer.lower().startswith("bearer "):
            token = bearer.split(" ", 1)[1]
        if wc.config.host:
            host = wc.config.host
    except Exception as exc:
        _LOG.warning("SDK ambient auth unavailable; falling back to notebook context", error=str(exc))

    # 2. Notebook context token (classic clusters)
    if (not token or not host) and dbutils is not None:
        try:
            ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
            if not token:
                token = ctx.apiToken().get()
            if not host:
                host = "https://" + ctx.browserHostName().get()
        except Exception as exc:
            _LOG.warning("notebook context token unavailable", error=str(exc))

    if not token or not host:
        raise RuntimeError(
            "Could not resolve workspace host/token. Run as a Job (run-as SP) with SDK "
            "ambient auth available, or on a cluster exposing the notebook context token."
        )

    return WorkspaceContext(workspace_url=host.rstrip("/"), token=token, account_id=account_id)


# ---------------------------------------------------------------------------
# Token providers
# ---------------------------------------------------------------------------

class StaticTokenProvider:
    """Wraps an already-issued token (the notebook-context token for this workspace)."""

    def __init__(self, token: str) -> None:
        self._token = token

    def __call__(self) -> str:
        return self._token


# Azure AD first-party application id for **AzureDatabricks**. Creating a Databricks secret scope
# that is BACKED BY an Azure Key Vault is the only call in this tool that a Databricks token cannot
# make: Databricks must prove to Azure that the CALLER may read that vault, and a Databricks
# OAuth/context token carries no Azure AD identity — the call fails with `"must have userAADToken
# defined!"`.
#
# IMP-4 (2026-08-08) — AKV-backed scope creation is NOT AUTOMATABLE in this customer's setup, proven
# live and stated deterministically:
#   • The only credential the customer can supply is a DATABRICKS SPN client id + secret. That mints
#     a DATABRICKS token (iss=<workspace>/oidc), NOT an Azure AD token (iss=login.microsoftonline.com,
#     aud=this app id) — different issuer, unusable as `userAADToken`.
#   • That SPN is backed by an Azure User-Assigned Managed Identity, which REFUSES secret-based Azure
#     AD auth entirely: `AADSTS7000232: MSI identity should not use ClientSecretCredential`. A MI only
#     gets tokens from Azure IMDS (169.254.169.254), reachable ONLY from Azure compute the MI is
#     attached to — and the workspace is front-end-private / VDI-only with no terminal.
# So there is no code path to an Azure AD token here. AKV-backed scopes are therefore reported as a
# clean MANUAL step (never attempted); Databricks-backed scopes migrate normally. The app id is kept
# only to name the exact resource in that manual remediation note.
AZURE_DATABRICKS_APP_ID = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"

# Refresh a client-credentials token this many seconds BEFORE it expires, so a long phase can't
# have a call fail on a token that aged out mid-flight.
_TOKEN_REFRESH_SKEW = 60


class OAuthM2MTokenProvider:
    """Client-credentials token provider — caches the token and refreshes before expiry.

    Used for the SOURCE workspace in `direct` mode: `POST {host}/oidc/v1/token` with
    `grant_type=client_credentials` and `scope=all-apis`. Thread-safe, because the content pass
    fetches notebooks from the source concurrently and they share one provider.

    Wrapped in the same `with_retry` as every other call, so a 429/5xx on the token endpoint is
    handled rather than failing the run.
    """

    def __init__(self, host: str, client_id: str, client_secret: str,
                 scope: str = "all-apis", verify_ssl: bool = True, timeout: int = 60) -> None:
        self._host = host.rstrip("/")
        self._client_id = client_id
        self._secret = client_secret
        self._scope = scope
        self._verify = verify_ssl
        self._timeout = timeout
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0

    @property
    def token_url(self) -> str:
        return f"{self._host}/oidc/v1/token"

    def _mint(self) -> tuple[str, float]:
        def _do():
            r = requests.post(
                self.token_url,
                auth=(self._client_id, self._secret),
                data={"grant_type": "client_credentials", "scope": self._scope},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                verify=self._verify, timeout=self._timeout)
            if is_retryable_status(r.status_code):
                retry_after = r.headers.get("Retry-After")
                raise RetryableHTTPError(
                    r.status_code, f"POST {self.token_url} -> {r.status_code}",
                    retry_after=float(retry_after) if retry_after else None)
            if r.status_code >= 400:
                # Deliberately does NOT echo the response body: an OAuth error body can quote the
                # request, and the secret must never reach a log.
                raise RuntimeError(
                    f"OAuth M2M token request failed: HTTP {r.status_code} from "
                    f"{self.token_url}. Check `source_sp_client_id`, the secret, and that the SP "
                    f"is a workspace admin on the source workspace.")
            doc = r.json()
            tok = doc.get("access_token") or ""
            if not tok:
                raise RuntimeError(f"OAuth M2M response from {self.token_url} carried no "
                                   f"access_token (keys: {sorted(doc)})")
            return tok, float(doc.get("expires_in") or 3600)

        token, expires_in = with_retry(_do)
        return token, time.time() + max(expires_in - _TOKEN_REFRESH_SKEW, 30)

    def __call__(self) -> str:
        with self._lock:
            if not self._token or time.time() >= self._expires_at:
                self._token, self._expires_at = self._mint()
                _LOG.info("minted OAuth M2M token for source workspace",
                          host=self._host, client_id=self._client_id,
                          valid_for_s=int(self._expires_at - time.time()))
            return self._token


def oauth_m2m_token_provider(host: str, client_id: str, client_secret: str,
                            scope: str = "all-apis") -> OAuthM2MTokenProvider:
    """Build a cached, self-refreshing client-credentials token provider for `host`."""
    return OAuthM2MTokenProvider(host, client_id, client_secret, scope=scope)


# NOTE: there is deliberately NO `mint_aad_token` here. Getting an Azure AD token for the
# AzureDatabricks app would require EITHER an Azure AD app-registration secret (the customer's
# Databricks SPN secret is a Databricks credential, not an Azure AD one) OR Azure IMDS (reachable
# only from Azure compute the managed identity is attached to, which the front-end-private / VDI /
# notebook-only setup does not provide). Both were proven impossible live (2026-08-08). So AKV-backed
# secret scopes are handled as a MANUAL step in the secrets importer — never minted, never attempted.


# ---------------------------------------------------------------------------
# REST client (bound to THIS workspace)
# ---------------------------------------------------------------------------

class ApiClient:
    """Thin authenticated REST client with retry, pagination, and SCIM helpers.

    Records pagination-truncation and fetch warnings on `.warnings` so the report can surface
    them instead of failing silently.
    """

    def __init__(self, host: str, token_provider, verify_ssl: bool = True, timeout: int = 60) -> None:
        self._base = host.rstrip("/")
        self._token_provider = token_provider
        self._verify = verify_ssl
        self._timeout = timeout
        self._s = requests.Session()
        self.warnings: list[str] = []

    @property
    def base_url(self) -> str:
        return self._base

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token_provider()}",
                "Content-Type": "application/json"}

    def auth_headers(self, content_type: str = "application/json") -> dict:
        """Fresh auth headers for a caller that must bypass this client's JSON request path.

        Public because two calls legitimately cannot go through `_request`: the streaming
        workspace-files upload (octet-stream body, path in the URL) and the AKV scope create (a
        one-off Azure AD bearer). Both still need THIS run's token — and a freshly-provided one, so a
        refreshed M2M token is picked up rather than a stale copy.
        """
        return {"Authorization": f"Bearer {self._token_provider()}",
                "Content-Type": content_type}

    def _request(self, method: str, path: str, *, params=None, json_body=None) -> Any:
        url = f"{self._base}/{path.lstrip('/')}"

        def _do():
            r = self._s.request(method, url, headers=self._headers(), params=params,
                                json=json_body, verify=self._verify, timeout=self._timeout)
            if is_retryable_status(r.status_code):
                retry_after = r.headers.get("Retry-After")
                raise RetryableHTTPError(
                    r.status_code,
                    f"{method} {url} -> {r.status_code}",
                    retry_after=float(retry_after) if retry_after else None,
                )
            if r.status_code >= 400:
                # `raise_for_status()` alone gives "400 Client Error: Bad Request for url: …", which
                # says nothing about WHAT was wrong. Databricks always explains itself in the body
                # (`error_code` + `message`), and that text is what makes an import failure
                # actionable — both for the operator reading the report and for classify_error(),
                # which matches on markers like RESOURCE_ALREADY_EXISTS. Without it every API
                # rejection looked identical.
                detail = ""
                try:
                    doc = r.json()
                    if isinstance(doc, dict):
                        detail = " ".join(str(doc.get(k)) for k in ("error_code", "message", "error")
                                          if doc.get(k))
                    detail = detail or r.text[:600]
                except ValueError:
                    detail = r.text[:600]
                raise HTTPStatusError(r.status_code,
                                      f"{method} {url} -> {r.status_code}: {detail[:600]}")
            if r.text:
                try:
                    return r.json()
                except ValueError:
                    return {"_raw": r.text}
            return {}

        return with_retry(_do)

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        return self._request("GET", path, params=params)

    def download_bytes(self, path: str, params: Optional[dict] = None, *,
                       max_bytes: int = 0) -> bytes:
        """GET a raw (non-JSON) body as bytes — for notebook/workspace-file CONTENT export.

        Streams the response so a large file isn't buffered twice. Retries on 429/5xx like every
        other call. `max_bytes>0` enforces a hard size cap: if the server advertises a larger
        Content-Length, OR the streamed body exceeds it, an `OversizeError` is raised (the caller
        turns that into a `skipped_oversize` record — Plan 2 §5a) instead of downloading a giant.
        Non-2xx responses raise (the body text is attached so callers can detect size errors).
        """
        url = f"{self._base}/{path.lstrip('/')}"

        def _do() -> bytes:
            with self._s.get(url, headers=self._headers(), params=params, verify=self._verify,
                             timeout=self._timeout, stream=True) as r:
                if is_retryable_status(r.status_code):
                    retry_after = r.headers.get("Retry-After")
                    raise RetryableHTTPError(
                        r.status_code, f"GET {url} -> {r.status_code}",
                        retry_after=float(retry_after) if retry_after else None)
                if r.status_code >= 400:
                    # Surface the server message (e.g. MAX_NOTEBOOK_SIZE_EXCEEDED) to the caller.
                    body = ""
                    try:
                        body = r.text[:2000]
                    except Exception:  # noqa: BLE001
                        pass
                    raise DownloadHTTPError(r.status_code, f"GET {url} -> {r.status_code}: {body}")
                if max_bytes:
                    clen = r.headers.get("Content-Length")
                    if clen and clen.isdigit() and int(clen) > max_bytes:
                        raise OversizeError(int(clen),
                                            f"Content-Length {clen} exceeds cap {max_bytes}")
                chunks: list[bytes] = []
                total = 0
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if max_bytes and total > max_bytes:
                        raise OversizeError(total, f"streamed body exceeds cap {max_bytes}")
                    chunks.append(chunk)
                return b"".join(chunks)

        return with_retry(_do)

    def post(self, path: str, body: dict) -> Any:
        return self._request("POST", path, json_body=body)

    def put(self, path: str, body: dict) -> Any:
        return self._request("PUT", path, json_body=body)

    def patch(self, path: str, body: dict, params: Optional[dict] = None) -> Any:
        # `params` carries query args some PATCH endpoints REQUIRE — notably Alerts V2
        # (`/api/2.0/alerts/{id}`) rejects a body-only PATCH with "update_mask is required".
        return self._request("PATCH", path, params=params, json_body=body)

    def delete(self, path: str, params: Optional[dict] = None) -> Any:
        return self._request("DELETE", path, params=params)

    # ── pagination helpers ────────────────────────────────────────────────
    def get_paginated(self, path: str, result_key: str, *, token_key: str = "next_page_token",
                      params: Optional[dict] = None, max_pages: int = 100_000) -> list:
        """Cursor pagination. `params` are sent on EVERY page (page_size, filters, etc.).

        Raises an explicit TRUNCATED warning (recorded on self.warnings) if the page cap is hit
        while a cursor still exists — never silently truncates.
        """
        base = dict(params or {})
        page_params = dict(base)
        items: list = []
        for page in range(max_pages):
            data = self.get(path, params=page_params)
            batch = data.get(result_key, []) if isinstance(data, dict) else []
            if isinstance(batch, list):
                items.extend(batch)
            cursor = data.get(token_key, "") if isinstance(data, dict) else ""
            if not cursor:
                return items
            page_params = dict(base)
            page_params["page_token"] = cursor
        msg = (f"{path}: hit the {max_pages}-page cap at {len(items)} items — MORE DATA EXISTS "
               f"and was NOT fetched (raise max_pages).")
        self.warnings.append(msg)
        _LOG.warning("pagination truncated", path=path, items=len(items))
        return items

    def get_scim(self, resource: str, max_items: int = 0, count: int = 500) -> list:
        """Paginate SCIM (Users/Groups/ServicePrincipals) via startIndex/count.

        max_items>0 caps the total fetched (for very large workspaces).
        """
        items: list = []
        start = 1
        while True:
            data = self.get(f"api/2.0/preview/scim/v2/{resource}",
                            params={"startIndex": start, "count": count})
            resources = data.get("Resources", []) if isinstance(data, dict) else []
            total = data.get("totalResults", 0) if isinstance(data, dict) else 0
            items.extend(resources)
            if max_items and len(items) >= max_items:
                return items[:max_items]
            if not resources or start + count - 1 >= total:
                return items
            start += count

    def list_existing(self, path: str, result_key: str, name_field: str = "name") -> set:
        """Return a set of existing resource names for duplicate checks (best-effort)."""
        try:
            data = self.get(path)
            items = data.get(result_key, []) if isinstance(data, dict) else []
            return {i.get(name_field, "") for i in items if i.get(name_field)}
        except Exception as exc:
            _LOG.warning("list_existing failed", path=path, error=str(exc))
            return set()


def build_client(config, dbutils=None, spark=None) -> ApiClient:
    """Return an ApiClient bound to THIS workspace. Populates config.ctx if not already set."""
    if not config.ctx.token or not config.ctx.workspace_url:
        config.ctx = resolve_context(dbutils, spark, account_id=config.ctx.account_id)
    return ApiClient(config.ctx.workspace_url, StaticTokenProvider(config.ctx.token))


def build_clients(config, dbutils=None, spark=None) -> tuple[ApiClient, ApiClient]:
    """Return `(source_client, target_client)` for this run's connectivity mode (Plan 3 §2).

    This is the ONE place the mode is handled. Collectors and importers already take a `client`
    argument, so they never learn which mode they're in — the runner decides which client to hand
    over. That containment is the whole point of putting the mode in one place.

      • `airgap`: both are the LOCAL context-token client. On the source side "this workspace" IS
        the source; on the target side the source client is never used (import reads the bundle).
      • `direct`: `source_client` is bound to `source_workspace_url` with an OAuth M2M token for
        the source SP; `target_client` is the local context-token client.
    """
    local = build_client(config, dbutils=dbutils, spark=spark)
    if not config.is_direct:
        return local, local

    secret = config.resolve_source_secret(dbutils)   # never logged, never stored
    provider = oauth_m2m_token_provider(config.source.workspace_url, config.source.client_id,
                                        secret)
    source_client = ApiClient(config.source.workspace_url, provider)
    _LOG.info("direct mode: source client bound",
              source_host=config.source.workspace_url,
              client_id=config.source.client_id,
              secret_from=("secret_scope" if config.source.uses_secret_scope else "widget"))
    return source_client, local


class MutationGuard:
    """Wraps an ApiClient and RAISES on any mutating verb — the dry-run purity assertion.

    `dry_run=true` is meant to be a full rehearsal: real reads, real bundle, real decisions, zero
    target writes. "The importers check a flag" is a claim; wrapping the client so a POST/PUT/
    PATCH/DELETE cannot physically happen is proof. Used by the dry-run tests and available to the
    notebook as a belt-and-braces option.

    GETs pass through untouched, so existence checks and the state decisions still run for real —
    which is what makes a dry run's report meaningful rather than a guess.
    """

    _MUTATING = ("post", "put", "patch", "delete")

    def __init__(self, inner) -> None:
        self._inner = inner
        self.attempted: list[str] = []

    def __getattr__(self, name: str):
        if name in self._MUTATING:
            def _blocked(path, *a, **kw):
                self.attempted.append(f"{name.upper()} {path}")
                raise AssertionError(
                    f"dry-run violation: {name.upper()} {path} — a dry run must make no "
                    f"mutating call")
            return _blocked
        return getattr(self._inner, name)
