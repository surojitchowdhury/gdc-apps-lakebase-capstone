"""Authentication clients for the Customer 360 Streamlit app.

Two distinct identities flow through this app:

* **OBO (on-behalf-of-user)** — carries the *calling user's* identity through
  the app to data services so that workspace-level access control and audit
  logs attribute activity to the real person. Built from the
  ``X-Forwarded-Access-Token`` header that the Databricks Apps proxy injects
  once the OBO preview + user consent are in place. Used for **SQL warehouse**
  and **Genie** calls only.
* **Service principal (SP)** — the app's own identity, provided by the Apps
  runtime via the default Databricks credential chain (env / config). Used for
  **all Lakebase access** and the **forward-ETL job trigger** — work that is
  not tied to an individual user.

Only platform-allowed OBO scopes are used: ``sql`` (SQL warehouse) and
``dashboards.genie`` (Genie API).

Config (host, profile) is read from the environment. When running locally /
headlessly, values are loaded from ``app/.env`` (never overriding values
already present in the environment, e.g. those injected by the Apps runtime).
"""

from __future__ import annotations

import os
from functools import lru_cache

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

from .config import host as _host  # shared env loading + typed host accessor

# OBO scopes this app requests. These are the only platform-allowed scopes we
# rely on: SQL warehouse access and the Genie API. Keep this in sync with the
# ``user_api_scopes`` declared in app.yaml.
OBO_SCOPES = ("sql", "dashboards.genie")

# Header names injected by the Databricks Apps proxy for the current request.
_HDR_ACCESS_TOKEN = "X-Forwarded-Access-Token"
_HDR_EMAIL = "X-Forwarded-Email"

# Env loading (app/.env, without overriding the Apps runtime) is centralised in
# :mod:`app.lib.config`, imported above; importing it runs ``load_env()`` once.


def _headers() -> "dict[str, str] | None":
    """Return the current inbound request headers via ``st.context.headers``.

    Returns ``None`` when Streamlit or a request context is unavailable (e.g.
    when this module is exercised headlessly from the CLI/SDK), so callers can
    degrade gracefully instead of crashing.
    """
    try:
        import streamlit as st
    except ModuleNotFoundError:
        return None
    try:
        # st.context.headers reflects the CURRENT inbound request. Read it live
        # inside the script run; never cache the token across reruns past TTL.
        return dict(st.context.headers)
    except Exception:
        # No active script/request context.
        return None


def _header(name: str) -> "str | None":
    """Case-insensitively fetch a single inbound request header, or ``None``."""
    headers = _headers()
    if not headers:
        return None
    # Header lookups should be case-insensitive.
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get(name.lower())


def obo_client() -> "WorkspaceClient | None":
    """Return a :class:`WorkspaceClient` acting as the **calling user** (OBO).

    Reads ``X-Forwarded-Access-Token`` from ``st.context.headers`` and builds a
    token-authenticated client bound to the workspace host. Used for SQL
    warehouse and Genie calls so activity is attributed to the real user.

    The header only appears once the workspace OBO preview ("User authorization
    (preview)") is enabled **and** the user has granted one-time consent. Until
    then this returns ``None`` — callers should surface a clear message (e.g.
    "user authorization not yet enabled") rather than fall back to the SP for
    user-scoped work. We build a fresh client per call so the token always
    reflects the current request and is never cached past its ~1h TTL.
    """
    token = _header(_HDR_ACCESS_TOKEN)
    if not token:
        return None
    # Pin unified auth to PAT/token: the Apps runtime also injects OAuth SP
    # credentials, but auth_type makes the SDK ignore those for this OBO client.
    cfg = Config(host=_host(), token=token, auth_type="pat")
    return WorkspaceClient(config=cfg)


@lru_cache(maxsize=1)
def sp_client() -> WorkspaceClient:
    """Return the app's **service-principal** :class:`WorkspaceClient` (cached).

    Uses the default Databricks credential chain: in production the Apps runtime
    provides the SP's OAuth credentials via the environment; locally it falls
    back to the configured CLI profile (``DATABRICKS_PROFILE`` from app/.env).
    This is the identity for all Lakebase access and the forward-ETL job
    trigger. Cached module-wide since the SP identity is stable for the app's
    lifetime (the SDK refreshes the underlying OAuth token internally).
    """
    profile = os.environ.get("DATABRICKS_PROFILE")
    if profile and not _running_in_apps_runtime():
        # Local / headless: authenticate via the CLI profile.
        return WorkspaceClient(profile=profile)
    # Production: the Apps runtime populates the default credential chain.
    return WorkspaceClient()


def _running_in_apps_runtime() -> bool:
    """Heuristic: are we running inside the Databricks Apps runtime?

    The runtime injects OAuth SP credentials via environment variables. When
    present we let the default credential chain pick them up rather than a local
    CLI profile.
    """
    return bool(os.environ.get("DATABRICKS_CLIENT_ID") and os.environ.get("DATABRICKS_CLIENT_SECRET"))


def current_user_email() -> "str | None":
    """Return the calling user's email from ``X-Forwarded-Email``.

    Used as the actor recorded in the app's audit log. Returns ``None`` when the
    header is absent (e.g. running headlessly, or before the OBO proxy injects
    identity headers) so callers can fall back to a placeholder actor.
    """
    return _header(_HDR_EMAIL)
