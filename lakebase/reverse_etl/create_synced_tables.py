#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.81", "psycopg[binary]>=3.2"]
# ///
"""T1 — Create the three Lakebase *synced* tables (reverse ETL from gold).

Idempotent & re-runnable: if a synced table already exists it is left in place
(we only report its status). Run with ``--recreate`` to drop & recreate.

Synced tables (UC name -> source, mode) in catalog suro_capstone_lb_sbx:
  suro_capstone_lb_sbx.public.customers_synced     <- gold.customers     CONTINUOUS
  suro_capstone_lb_sbx.public.transactions_synced  <- gold.transactions  CONTINUOUS
  suro_capstone_lb_sbx.public.products_synced      <- gold.products      TRIGGERED (hourly)

--- Sync-mode rationale (see README.md) -----------------------------------
customers & transactions are CONTINUOUS: the app must reflect upstream churn
scores, lifetime-value and new transactions within seconds, so we pay for a
always-on streaming pipeline (min ~15s latency).

products is TRIGGERED on an hourly schedule: the product catalog is
slow-changing (price/stock edits are infrequent, 200 rows), so a continuously
running pipeline would waste compute. An hourly TRIGGERED refresh is markedly
cheaper and well within the freshness this dimension table needs.

Prefer a declarative synced-table spec (captured later in a bundle for T8);
this SDK script is the T1-acceptable equivalent and emits the equivalent spec.

Usage:
  uv run lakebase/reverse_etl/create_synced_tables.py [--recreate] [--no-wait]
"""

from __future__ import annotations

import argparse
import time

from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service.database import (
    DatabaseCatalog,
    SyncedDatabaseTable,
    SyncedTableSchedulingPolicy,
    SyncedTableSpec,
)

import _common as C

# The UC schema under the Lakebase-registered catalog. Synced tables land in
# Postgres database=PGDATABASE, schema=public.
PG_SCHEMA = "public"

# TRIGGERED refresh cadence for the slow-changing products catalog.
PRODUCTS_TRIGGER_CRON = "0 0 * * * ?"  # top of every hour (quartz cron)

# name (unqualified) -> (source gold table, primary keys, scheduling policy)
SYNCED_TABLES = [
    ("customers_synced", "customers", ["customer_id"], SyncedTableSchedulingPolicy.CONTINUOUS),
    (
        "transactions_synced",
        "transactions",
        ["transaction_id"],
        SyncedTableSchedulingPolicy.CONTINUOUS,
    ),
    # TRIGGERED (hourly) — slow-changing catalog; cheaper than an always-on pipeline.
    ("products_synced", "products", ["product_id"], SyncedTableSchedulingPolicy.TRIGGERED),
]

# Terminal healthy states per policy.
_HEALTHY = {
    "CONTINUOUS": {"SYNCED_TABLE_ONLINE_CONTINUOUS_UPDATE", "SYNCED_TABLE_ONLINE"},
    "TRIGGERED": {
        "SYNCED_TABLE_ONLINE_NO_PENDING_UPDATE",
        "SYNCED_TABLE_ONLINE_TRIGGERED_UPDATE",
        "SYNCED_TABLE_ONLINE",
    },
}


class CatalogUnavailable(RuntimeError):
    """Raised when no Lakebase *database catalog* is available and we lack the
    privilege to register one. Synced tables cannot be created without it; the
    caller documents the blocker and exits cleanly (see README 'Deferred')."""


def ensure_catalog(w) -> str:
    """Ensure the UC database catalog exists & is registered against the
    Lakebase instance. Returns the catalog name.

    Registering a database catalog requires ``CREATE CATALOG`` on the metastore.
    In this workspace the owner of ``capstone-pg`` is a workspace admin but NOT a
    metastore admin, so if the catalog is not already registered we raise
    :class:`CatalogUnavailable` rather than dying on an uncaught traceback."""
    catalog = C.pg_uc_catalog()
    instance = C.pg_instance_name()
    database = C.pg_database()
    try:
        existing = w.database.get_database_catalog(catalog)
        print(f"[catalog] {catalog} already registered "
              f"(instance={existing.database_instance_name}, db={existing.database_name})")
        return catalog
    except (NotFound, PermissionDenied):
        # Not registered yet (a non-existent UC catalog surfaces as
        # PermissionDenied "not accessible"). Try to register it now.
        pass

    print(f"[catalog] registering {catalog} -> instance={instance} db={database}")
    try:
        w.database.create_database_catalog(
            DatabaseCatalog(
                name=catalog,
                database_instance_name=instance,
                database_name=database,
                create_database_if_not_exists=True,
            )
        )
    except PermissionDenied as e:
        raise CatalogUnavailable(str(e)) from e
    return catalog


def uc_target(catalog: str, name: str) -> str:
    return f"{catalog}.{PG_SCHEMA}.{name}"


def create_one(w, catalog: str, name: str, source: str, pks, policy, *, recreate: bool) -> None:
    target = uc_target(catalog, name)
    src_full = C.uc_source(source)
    if recreate:
        try:
            w.database.delete_synced_database_table(target)
            print(f"[{name}] deleted existing (recreate)")
            time.sleep(3)
        except NotFound:
            pass

    try:
        w.database.get_synced_database_table(target)
        print(f"[{name}] already exists — leaving in place (use --recreate to rebuild)")
        return
    except (NotFound, PermissionDenied):
        pass

    spec = SyncedTableSpec(
        source_table_full_name=src_full,
        primary_key_columns=list(pks),
        scheduling_policy=policy,
        create_database_objects_if_missing=True,
    )
    print(f"[{name}] creating {policy.value} synced table <- {src_full}")
    w.database.create_synced_database_table(
        SyncedDatabaseTable(
            name=target,
            database_instance_name=C.pg_instance_name(),
            logical_database_name=C.pg_database(),
            spec=spec,
        )
    )


def ensure_hourly_trigger_job(w, catalog: str) -> None:
    """A TRIGGERED synced table has an underlying DLT pipeline that only
    refreshes when invoked. Schedule that refresh hourly via a Databricks Job
    with a quartz-cron trigger, so products_synced stays at most ~1h stale.

    Idempotent: reuses the job if it already exists (matched by name).
    """
    from databricks.sdk.service import jobs

    st = w.database.get_synced_database_table(uc_target(catalog, "products_synced"))
    pipeline_id = st.data_synchronization_status.pipeline_id if st.data_synchronization_status else None
    if not pipeline_id:
        print("[job] products_synced pipeline_id not available yet — skipping job creation")
        return

    job_name = "capstone-products_synced-hourly-refresh"
    existing = next((j for j in w.jobs.list(name=job_name)), None)
    task = jobs.Task(
        task_key="refresh_products_synced",
        pipeline_task=jobs.PipelineTask(pipeline_id=pipeline_id),
    )
    schedule = jobs.CronSchedule(
        quartz_cron_expression=PRODUCTS_TRIGGER_CRON,
        timezone_id="UTC",
        pause_status=jobs.PauseStatus.UNPAUSED,
    )
    if existing:
        print(f"[job] {job_name} exists (id={existing.job_id}) — updating schedule/pipeline")
        w.jobs.reset(
            job_id=existing.job_id,
            new_settings=jobs.JobSettings(name=job_name, tasks=[task], schedule=schedule),
        )
    else:
        created = w.jobs.create(name=job_name, tasks=[task], schedule=schedule)
        print(f"[job] created hourly refresh job id={created.job_id} cron={PRODUCTS_TRIGGER_CRON!r}")


def wait_until_healthy(w, catalog: str, timeout_s: int = 1800) -> dict:
    """Poll all synced tables until each reaches a healthy terminal state
    (or timeout). Returns {name: detailed_state}."""
    deadline = time.time() + timeout_s
    names = [t[0] for t in SYNCED_TABLES]
    policy_by_name = {t[0]: t[3].value for t in SYNCED_TABLES}
    last: dict[str, str] = {}
    while True:
        done = True
        for name in names:
            st = w.database.get_synced_database_table(uc_target(catalog, name))
            state = (st.data_synchronization_status.detailed_state.value
                     if st.data_synchronization_status
                     and st.data_synchronization_status.detailed_state else "UNKNOWN")
            last[name] = state
            if state not in _HEALTHY[policy_by_name[name]] and "FAILED" not in state:
                done = False
        stamp = ", ".join(f"{n}={last[n]}" for n in names)
        print(f"[wait] {stamp}")
        if done or time.time() > deadline:
            return last
        time.sleep(20)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recreate", action="store_true", help="drop & recreate synced tables")
    ap.add_argument("--no-wait", action="store_true", help="don't poll for healthy state")
    args = ap.parse_args()

    C.load_env()
    w = C.workspace_client()
    try:
        catalog = ensure_catalog(w)
    except CatalogUnavailable as e:
        # No Lakebase database catalog is available and we cannot register one
        # (metastore-admin CREATE CATALOG required). Synced tables are DEFERRED,
        # not failed: this script stays idempotent and re-runnable. Once a
        # database catalog exists (see below), re-run and the tables provision.
        cat = C.pg_uc_catalog()
        print("\n" + "=" * 72)
        print("[DEFERRED] Synced tables NOT created — no Lakebase database catalog.")
        print("=" * 72)
        print(f"Blocker: {e}".rstrip())
        print(
            "\nTo unblock, a metastore admin must register a database catalog "
            f"against the\ninstance {C.pg_instance_name()!r}. Then re-run this "
            "script unchanged.\n\nExact command (run as a metastore admin, or ask "
            "one to run it):\n"
            f"  databricks database create-database-catalog {cat} "
            f"{C.pg_instance_name()} {C.pg_database()} \\\n"
            f"      --create-database-if-not-exists -p $DATABRICKS_PROFILE\n"
            "\nThen:\n"
            "  uv run lakebase/reverse_etl/create_synced_tables.py\n"
        )
        raise SystemExit(3)

    for name, source, pks, policy in SYNCED_TABLES:
        create_one(w, catalog, name, source, pks, policy, recreate=args.recreate)

    if args.no_wait:
        print("[done] created (skipping wait)")
        return

    final = wait_until_healthy(w, catalog)
    # products_synced is TRIGGERED: schedule its pipeline to refresh hourly.
    ensure_hourly_trigger_job(w, catalog)
    print("\n=== Final synced-table states ===")
    for name, source, pks, policy in SYNCED_TABLES:
        print(f"  {catalog}.{PG_SCHEMA}.{name:<20} {policy.value:<11} -> {final.get(name)}")


if __name__ == "__main__":
    main()
