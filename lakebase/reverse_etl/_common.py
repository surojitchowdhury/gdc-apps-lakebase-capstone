"""Shared config + connection helpers for the T1 reverse-ETL scripts.

All values are read from environment variables (populated from ``app/.env`` by
:func:`load_env`) so that no host names, catalog names, or secrets are ever
hard-coded. Every script in this package imports from here.

Connections to Lakebase Postgres use a short-lived (~1h) Databricks OAuth token
as the password, generated via the SDK. We never store or log the token.
"""

from __future__ import annotations

import os
import socket
import uuid
from pathlib import Path

# --- Locate app/.env (repo-root/app/.env) regardless of CWD -----------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _REPO_ROOT / "app" / ".env"


def load_env() -> None:
    """Load key=value pairs from app/.env into os.environ (does not override
    values already present in the environment)."""
    if not _ENV_FILE.exists():
        return
    for raw in _ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        os.environ.setdefault(key, value)


def env(name: str, default: str | None = None, *, required: bool = False) -> str:
    """Fetch an env var, optionally requiring it to be set."""
    val = os.environ.get(name, default)
    if required and not val:
        raise SystemExit(f"Required environment variable {name!r} is not set (check app/.env)")
    return val  # type: ignore[return-value]


# --- Config accessors -------------------------------------------------------
def profile() -> str:
    return env("DATABRICKS_PROFILE", required=True)


def pg_instance_name() -> str:
    return env("PG_INSTANCE_NAME", "capstone-pg")


def pg_database() -> str:
    return env("PGDATABASE", "capstone_db")


def pg_host() -> str:
    return env("PGHOST", required=True)


def uc_source(table: str) -> str:
    """Fully-qualified gold source table name.

    NOTE: the spec writes ``<catalog>.gold.<table>`` generically, but in THIS
    workspace the 5 gold tables live in ``<catalog>.<CAPSTONE_SCHEMA>`` (schema
    ``lakebase_app_capstone``), not a schema literally named ``gold``.
    """
    catalog = env("CAPSTONE_CATALOG", required=True)
    schema = env("CAPSTONE_SCHEMA", required=True)
    return f"{catalog}.{schema}.{table}"


def pg_uc_catalog() -> str:
    """UC catalog registered against the Lakebase instance. Synced tables are
    created as ``<pg_uc_catalog>.public.<name>`` in UC, which surfaces in
    Postgres under database ``PGDATABASE`` / schema ``public``.

    The serverless-sandbox catalog is ``suro_capstone_lb_sbx`` (bound to
    instance ``capstone-pg`` / db ``capstone_db``). Do NOT use
    ``capstone_lakebase`` — that is a different user's catalog on another
    workspace and throws 'Cross workspace access is not allowed'."""
    return env("PG_UC_CATALOG", "suro_capstone_lb_sbx")


def workspace_client():
    """Build a WorkspaceClient bound to the configured CLI profile."""
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient(profile=profile())


def pg_connect(*, autocommit: bool = True):
    """Open a psycopg3 connection to Lakebase using a fresh OAuth token.

    Returns a live ``psycopg.Connection``. Caller is responsible for closing
    (use as a context manager). We resolve the host to an IP and pass
    ``hostaddr`` to work around intermittent DNS resolution on macOS.
    """
    import psycopg

    w = workspace_client()
    cred = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[pg_instance_name()],
    )
    user = w.current_user.me().user_name
    host = pg_host()
    try:
        hostaddr = socket.gethostbyname(host)
    except socket.gaierror:
        hostaddr = None

    conninfo = {
        "host": host,
        "dbname": pg_database(),
        "user": user,
        "password": cred.token,
        "sslmode": "require",
    }
    if hostaddr:
        conninfo["hostaddr"] = hostaddr

    return psycopg.connect(" ".join(f"{k}={v}" for k, v in conninfo.items()), autocommit=autocommit)
