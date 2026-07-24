"""Lakebase (Postgres) connectivity for the Customer 360 app.

All in-app database access runs as the app's **service principal** (SP). We
mint a fresh short-lived (~1h) Databricks OAuth credential per checkout via the
SDK and use it as the Postgres password over an ``sslmode=require`` connection.

Why there is NO ``lakebase_obo()``
----------------------------------
Lakebase does not (yet) support OBO scopes. Calling
``generate_database_credential`` with a *user* OBO bearer token fails with::

    Provided OAuth token does not have required scopes: postgres

So every Lakebase read/write goes through the SP. The calling user's identity
is captured separately from ``X-Forwarded-Email`` (see
:func:`app.lib.auth.current_user_email`) and written into the audit-log columns
of each staging write — the *actor* is recorded even though the *connection*
is the SP. Adding a ``lakebase_obo()`` would only ever raise, so it is
deliberately omitted.

Token handling: we mint a new credential on every ``lakebase_sp()`` checkout
(the mint is cheap relative to the query) and never cache a token past its TTL.
A connection pool with token rotation is a valid alternative for higher
throughput; per-checkout minting is used here for simplicity and correctness.
"""

from __future__ import annotations

import socket
import uuid

import psycopg

from .auth import sp_client


def _pg_env(name: str) -> str:
    """Fetch a required Postgres connection env var (loaded from app/.env)."""
    import os

    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set (check app/.env)."
        )
    return val


def lakebase_sp(*, autocommit: bool = True) -> psycopg.Connection:
    """Open a psycopg connection to Lakebase as the app **service principal**.

    Mints a FRESH ~1h OAuth Lakebase credential for this checkout via
    ``sp_client().database.generate_database_credential`` and uses it as the
    Postgres password. Returns a live :class:`psycopg.Connection`; the caller is
    responsible for closing it (use it as a context manager).

    The connection is TLS-only (``sslmode=require``). We resolve the host to an
    IP and pass ``hostaddr`` to sidestep intermittent DNS resolution issues on
    some client machines while keeping SNI/cert validation against ``host``.
    """
    w = sp_client()

    # Fresh, short-lived credential minted per checkout — never cached past TTL.
    cred = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[_pg_env("PG_INSTANCE_NAME")],
    )

    # The Postgres role is the SP's own identity (the credential is scoped to it).
    user = w.current_user.me().user_name

    host = _pg_env("PGHOST")
    try:
        hostaddr = socket.gethostbyname(host)
    except socket.gaierror:
        hostaddr = None

    conninfo = {
        "host": host,
        "dbname": _pg_env("PGDATABASE"),
        "user": user,
        "password": cred.token,
        "sslmode": "require",
    }
    if hostaddr:
        conninfo["hostaddr"] = hostaddr

    return psycopg.connect(
        " ".join(f"{k}={v}" for k, v in conninfo.items()),
        autocommit=autocommit,
    )
