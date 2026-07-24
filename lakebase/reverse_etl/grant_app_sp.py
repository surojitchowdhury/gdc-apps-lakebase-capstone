#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.81", "psycopg[binary]>=3.2"]
# ///
"""T1 — One-time GRANTs for the app service principal's Postgres role.

Fresh Lakebase PG roles have NO privileges on synced/staging tables, so the
app SP cannot read or write until we grant it. This script grants:

  * SELECT                 on the 3 synced tables (customers/transactions/products)
  * SELECT, INSERT, UPDATE on the 3 staging tables
  * USAGE, SELECT          on all sequences in `public` (for IDENTITY columns)
  * ALTER DEFAULT PRIVILEGES so FUTURE tables/sequences (e.g. new synced tables)
    automatically grant the SP the right privileges.

IMPORTANT — RUN ORDER:
  The SP's PG role only exists AFTER the app service principal has logged into
  Lakebase at least once (its role name is the SP's client_id UUID). Run this
  AFTER that first login, otherwise the GRANT fails with "role does not exist".

The SP role name is a parameter — pass --sp-role or set APP_SP_PG_ROLE. We never
hard-code a UUID. Role identifiers are validated and quoted with
psycopg.sql.Identifier (never f-strung) to prevent injection.

Usage:
  uv run lakebase/reverse_etl/grant_app_sp.py --sp-role <client_id-uuid>
  # or:  APP_SP_PG_ROLE=<uuid> uv run lakebase/reverse_etl/grant_app_sp.py
  # dry-run (print SQL, don't execute):  --dry-run
"""

from __future__ import annotations

import argparse
import os

from psycopg import sql

import _common as C

SYNCED_TABLES = ("customers_synced", "transactions_synced", "products_synced")
STAGING_TABLES = (
    "customer_notes_staging",
    "customer_segment_overrides_staging",
    "customer_audit_log",
)


def build_statements(role: str) -> list[sql.Composed]:
    """Build the parameterized GRANT statements. `role` is passed through
    sql.Identifier so it is safely quoted, never interpolated as raw text."""
    r = sql.Identifier(role)
    stmts: list[sql.Composed] = []

    # Connect + schema usage.
    stmts.append(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
        sql.Identifier(C.pg_database()), r))
    stmts.append(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(r))

    # SELECT on synced tables (read-only).
    for t in SYNCED_TABLES:
        stmts.append(sql.SQL("GRANT SELECT ON TABLE public.{} TO {}").format(
            sql.Identifier(t), r))

    # SELECT/INSERT/UPDATE on staging tables (writable; no DELETE — audit log
    # is append-only and staging rows are marked processed, not removed).
    for t in STAGING_TABLES:
        stmts.append(sql.SQL("GRANT SELECT, INSERT, UPDATE ON TABLE public.{} TO {}").format(
            sql.Identifier(t), r))

    # Sequences behind IDENTITY columns.
    stmts.append(sql.SQL(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(r))

    # Future-proofing: default privileges so newly-created synced tables and
    # sequences in `public` inherit SELECT / sequence usage for the SP.
    stmts.append(sql.SQL(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(r))
    stmts.append(sql.SQL(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {}").format(r))

    return stmts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sp-role", default=os.environ.get("APP_SP_PG_ROLE"),
                    help="App service principal PG role name (its client_id UUID)")
    ap.add_argument("--dry-run", action="store_true", help="print SQL without executing")
    args = ap.parse_args()

    C.load_env()
    role = (args.sp_role or "").strip()
    if not role:
        raise SystemExit(
            "Provide the app SP PG role via --sp-role or APP_SP_PG_ROLE "
            "(it is the service principal's client_id UUID). "
            "Run this only AFTER the SP has logged into Lakebase once.")

    stmts = build_statements(role)

    if args.dry_run:
        print(f"-- DRY RUN: {len(stmts)} statements for role {role!r}")
        # Render for display only using a throwaway connection-less context.
        for s in stmts:
            print(s.as_string(None) + ";")
        return

    with C.pg_connect() as conn, conn.cursor() as cur:
        # Confirm the role exists before granting. A missing role is an EXPECTED
        # pre-deploy state (the SP's PG role only appears after its first Lakebase
        # login), so we skip cleanly with exit code 0 rather than failing — this
        # keeps the step safe to run unconditionally in automation.
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cur.fetchone() is None:
            print(
                f"[grants] SKIPPED — PG role {role!r} does not exist yet. The app "
                "service principal must log into Lakebase at least once before "
                "grants can be applied. Re-run this script after the app's first "
                "login. (This is not an error.)")
            return
        for s in stmts:
            cur.execute(s)
        print(f"[grants] applied {len(stmts)} GRANT/ALTER DEFAULT PRIVILEGES statements "
              f"to role {role!r}")


if __name__ == "__main__":
    main()
