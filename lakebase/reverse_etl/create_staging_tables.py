#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.81", "psycopg[binary]>=3.2"]
# ///
"""T1 — Create the three writable *staging* tables in Lakebase (capstone_db).

These tables live only in Postgres (they are NOT synced from gold): the app
writes customer notes, segment overrides, and an audit trail here without ever
touching the read-only gold data.

DDL is idempotent (CREATE TABLE / INDEX IF NOT EXISTS) and re-runnable. All
identifiers are constant literals defined in this file — no user input is ever
interpolated into SQL.

Tables:
  * customer_notes_staging
      Free-text notes an agent leaves on a customer. `processed` lets a
      downstream job pick up unhandled notes (forward-ETL back to gold, T-later).
  * customer_segment_overrides_staging
      Manual segment reassignment. PK on customer_id makes the override
      idempotent: re-submitting for the same customer UPSERTs one row.
  * customer_audit_log
      Append-only trail of every mutating action the app performs. `detail`
      is jsonb for structured, queryable context.

Usage:
  uv run lakebase/reverse_etl/create_staging_tables.py
"""

from __future__ import annotations

import _common as C

# Idempotent DDL. Kept as constant strings (no interpolation of external input).
DDL_STATEMENTS = [
    # --- customer_notes_staging ------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS customer_notes_staging (
        id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        customer_id  TEXT        NOT NULL,
        note         TEXT        NOT NULL,
        actor_email  TEXT        NOT NULL,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        processed    BOOLEAN     NOT NULL DEFAULT false
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_notes_customer ON customer_notes_staging (customer_id)",
    "CREATE INDEX IF NOT EXISTS ix_notes_unprocessed ON customer_notes_staging (processed) WHERE processed = false",
    # --- customer_segment_overrides_staging ------------------------------
    # PK on customer_id => at most one active override per customer; the app
    # UPSERTs (ON CONFLICT (customer_id) DO UPDATE) so overrides are idempotent.
    """
    CREATE TABLE IF NOT EXISTS customer_segment_overrides_staging (
        customer_id  TEXT        PRIMARY KEY,
        segment_id   TEXT        NOT NULL,
        actor_email  TEXT        NOT NULL,
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        processed    BOOLEAN     NOT NULL DEFAULT false
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_overrides_unprocessed ON customer_segment_overrides_staging (processed) WHERE processed = false",
    # --- customer_audit_log (append-only) --------------------------------
    """
    CREATE TABLE IF NOT EXISTS customer_audit_log (
        id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        actor_email  TEXT        NOT NULL,
        action       TEXT        NOT NULL,
        customer_id  TEXT,
        detail       JSONB       NOT NULL DEFAULT '{}'::jsonb,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_audit_customer ON customer_audit_log (customer_id)",
    "CREATE INDEX IF NOT EXISTS ix_audit_created ON customer_audit_log (created_at)",
]

STAGING_TABLES = (
    "customer_notes_staging",
    "customer_segment_overrides_staging",
    "customer_audit_log",
)


def main() -> None:
    C.load_env()
    with C.pg_connect() as conn, conn.cursor() as cur:
        for stmt in DDL_STATEMENTS:
            cur.execute(stmt)
        print(f"[staging] applied {len(DDL_STATEMENTS)} idempotent DDL statements")

        # Verify: list tables + columns from information_schema.
        print("\n=== Staging tables (information_schema) ===")
        for tbl in STAGING_TABLES:
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
                """,
                (tbl,),
            )
            rows = cur.fetchall()
            if not rows:
                print(f"  !! {tbl}: NOT FOUND")
                continue
            print(f"\n  {tbl}:")
            for col, dtype, nullable, default in rows:
                d = f" DEFAULT {default}" if default else ""
                print(f"    - {col:<14} {dtype:<28} {'NULL' if nullable == 'YES' else 'NOT NULL'}{d}")


if __name__ == "__main__":
    main()
