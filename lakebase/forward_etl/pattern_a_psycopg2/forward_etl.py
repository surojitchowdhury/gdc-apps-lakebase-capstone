# Databricks notebook source
# ruff: noqa: E402, F821
#   E402: notebook cells import where needed, not all at the top of the file.
#   F821: `spark` and `dbutils` are globals injected by the Databricks runtime.
# This leading code cell is comments-only; the notebook content begins in the
# next cell. Keeping the ruff directive in its OWN cell preserves the Databricks
# notebook structure (a markdown cell must START with `# MAGIC %md`).

# COMMAND ----------

# MAGIC %md
# MAGIC # Forward ETL — Pattern A (psycopg + MERGE INTO Delta)
# MAGIC
# MAGIC **Pull-based, on-demand.** Promotes app-written Lakebase *staging* rows
# MAGIC into UC gold Delta tables. Triggered on demand by the Streamlit app's
# MAGIC Reports page ("Run forward-ETL") via the Jobs API.
# MAGIC
# MAGIC ## Flow (per staging table)
# MAGIC 1. Connect to Lakebase Postgres via **psycopg** as the job's run-as
# MAGIC    identity (the **app service principal** once bound at T8; the current
# MAGIC    user in the sandbox until then). A fresh ~1h Databricks OAuth token is
# MAGIC    minted per run via the SDK and used as the Postgres password over an
# MAGIC    `sslmode=require` connection — never stored or logged.
# MAGIC 2. `SELECT ... WHERE processed = false` — read only unconsumed rows.
# MAGIC 3. Build a Spark DataFrame and **`MERGE INTO`** the gold Delta target on
# MAGIC    the table's primary key (idempotent: re-running merges the same rows to
# MAGIC    the same result).
# MAGIC 4. Only after the MERGE commits, `UPDATE *_staging SET processed = true
# MAGIC    WHERE id IN (...)` for **exactly** the rows merged.
# MAGIC
# MAGIC ## Ordering / crash safety
# MAGIC The Delta MERGE runs **before** the staging `UPDATE`. A failure in between
# MAGIC leaves gold updated but staging still `processed = false` — the next run
# MAGIC simply re-merges those rows (MERGE on PK is idempotent) and then marks them
# MAGIC processed. So a partial failure never loses data and never double-writes;
# MAGIC the whole job is safe to re-run.
# MAGIC
# MAGIC ## Gold target mapping
# MAGIC | Staging table | Gold Delta target | MERGE key |
# MAGIC |---|---|---|
# MAGIC | `customer_notes_staging` | `<catalog>.<schema>.customer_notes` | `id` (staging IDENTITY PK) |
# MAGIC | `customer_segment_overrides_staging` | `<catalog>.<schema>.customer_segment_overrides` | `customer_id` |
# MAGIC
# MAGIC Segment overrides are materialised into a **dedicated gold override table**
# MAGIC (not by mutating `customers.segment`) so the forward-ETL never fights the
# MAGIC reverse-ETL sync that keeps `customers` fresh from gold; analytics can join
# MAGIC the override table to `customers` to get the effective segment.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 0 · Dependencies
# MAGIC `psycopg` (v3) is the Lakebase Postgres driver. Serverless notebooks start
# MAGIC without it, so install it at run time.

# COMMAND ----------

# MAGIC %pip install "psycopg[binary]>=3.2" databricks-sdk>=0.40
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1 · Parameters
# MAGIC Every operational value comes from a job/notebook parameter (or a
# MAGIC documented sensible default). No hard-coded hosts, catalogs, or secrets.

# COMMAND ----------

import uuid

# Job parameters (dbutils widgets). Defaults match the capstone sandbox and are
# documented in lakebase/forward_etl/pattern_a_psycopg2/README.md; override them
# via job parameters for another workspace/instance.
dbutils.widgets.text("catalog", "suro_cat", "Gold UC catalog")
dbutils.widgets.text("schema", "app_capstone", "Gold UC schema")
dbutils.widgets.text("pg_instance", "capstone-pg", "Lakebase instance name")
dbutils.widgets.text("pg_database", "capstone_db", "Lakebase Postgres database")
dbutils.widgets.text(
    "pg_host",
    "ep-lucky-moon-d1e6mir6.database.us-west-2.cloud.databricks.com",
    "Lakebase Postgres host",
)

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
PG_INSTANCE = dbutils.widgets.get("pg_instance").strip()
PG_DATABASE = dbutils.widgets.get("pg_database").strip()
PG_HOST = dbutils.widgets.get("pg_host").strip()

GOLD_NOTES = f"{CATALOG}.{SCHEMA}.customer_notes"
GOLD_OVERRIDES = f"{CATALOG}.{SCHEMA}.customer_segment_overrides"

print(f"Gold notes target     : {GOLD_NOTES}")
print(f"Gold overrides target : {GOLD_OVERRIDES}")
print(f"Lakebase             : {PG_INSTANCE} / {PG_DATABASE} @ {PG_HOST}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2 · Lakebase connection (psycopg, OAuth token as password)
# MAGIC Mirrors `app/lib/db.py` / `lakebase/reverse_etl/_common.py`: mint a fresh
# MAGIC short-lived credential via the SDK, connect with `sslmode=require`. The
# MAGIC connection identity is the job's run-as (app SP once bound at T8).

# COMMAND ----------

import psycopg
from databricks.sdk import WorkspaceClient


def lakebase_connect(*, autocommit: bool = True) -> psycopg.Connection:
    """Open a psycopg connection to Lakebase using a fresh OAuth token.

    The token is minted for the notebook's run-as identity and used as the
    Postgres password. TLS-only (sslmode=require); the token is never stored
    or logged.
    """
    w = WorkspaceClient()
    cred = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[PG_INSTANCE],
    )
    user = w.current_user.me().user_name
    conninfo = {
        "host": PG_HOST,
        "dbname": PG_DATABASE,
        "user": user,
        "password": cred.token,
        "sslmode": "require",
    }
    return psycopg.connect(
        " ".join(f"{k}={v}" for k, v in conninfo.items()),
        autocommit=autocommit,
    )


# COMMAND ----------

# MAGIC %md
# MAGIC ## 3 · Ensure gold Delta targets exist (idempotent)
# MAGIC `CREATE TABLE IF NOT EXISTS` — safe to run every time. `processed_at`
# MAGIC records when forward-ETL last materialised each row.

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")

spark.sql(
    f"""
    CREATE TABLE IF NOT EXISTS {GOLD_NOTES} (
        id           BIGINT,
        customer_id  STRING,
        note         STRING,
        actor_email  STRING,
        created_at   TIMESTAMP,
        processed_at TIMESTAMP
    ) USING DELTA
    COMMENT 'Gold customer notes materialised from customer_notes_staging (forward-ETL Pattern A).'
    """
)

spark.sql(
    f"""
    CREATE TABLE IF NOT EXISTS {GOLD_OVERRIDES} (
        customer_id  STRING,
        segment_id   STRING,
        actor_email  STRING,
        updated_at   TIMESTAMP,
        processed_at TIMESTAMP
    ) USING DELTA
    COMMENT 'Gold customer segment overrides materialised from customer_segment_overrides_staging (forward-ETL Pattern A).'
    """
)

print("Gold targets ready.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4 · Forward-ETL helper (read → MERGE → mark processed)

# COMMAND ----------

from datetime import datetime, timezone

from pyspark.sql import Row
from pyspark.sql import functions as F
from pyspark.sql.types import (
    LongType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

NOTES_SCHEMA = StructType(
    [
        StructField("id", LongType(), False),
        StructField("customer_id", StringType(), True),
        StructField("note", StringType(), True),
        StructField("actor_email", StringType(), True),
        StructField("created_at", TimestampType(), True),
    ]
)

OVERRIDES_SCHEMA = StructType(
    [
        StructField("customer_id", StringType(), False),
        StructField("segment_id", StringType(), True),
        StructField("actor_email", StringType(), True),
        StructField("updated_at", TimestampType(), True),
    ]
)


def forward_etl_notes(conn) -> dict:
    """Promote unprocessed notes → gold, then mark them processed."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, customer_id, note, actor_email, created_at "
            "FROM customer_notes_staging WHERE processed = false ORDER BY id"
        )
        rows = cur.fetchall()

    read_n = len(rows)
    if read_n == 0:
        return {"read": 0, "merged": 0, "marked": 0}

    now = datetime.now(timezone.utc)
    df = spark.createDataFrame(
        [Row(id=r[0], customer_id=r[1], note=r[2], actor_email=r[3], created_at=r[4]) for r in rows],
        schema=NOTES_SCHEMA,
    ).withColumn("processed_at", F.lit(now).cast("timestamp"))
    df.createOrReplaceTempView("_notes_updates")

    # MERGE first (idempotent on the staging PK `id`).
    spark.sql(
        f"""
        MERGE INTO {GOLD_NOTES} t
        USING _notes_updates s ON t.id = s.id
        WHEN MATCHED THEN UPDATE SET
            t.customer_id = s.customer_id,
            t.note        = s.note,
            t.actor_email = s.actor_email,
            t.created_at  = s.created_at,
            t.processed_at = s.processed_at
        WHEN NOT MATCHED THEN INSERT
            (id, customer_id, note, actor_email, created_at, processed_at)
            VALUES (s.id, s.customer_id, s.note, s.actor_email, s.created_at, s.processed_at)
        """
    )

    # Then mark exactly those ids processed. A crash before this leaves gold
    # updated + staging unprocessed => next run re-merges (idempotent) and marks.
    ids = [r[0] for r in rows]
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE customer_notes_staging SET processed = true WHERE id = ANY(%s)",
            (ids,),
        )
        marked = cur.rowcount
    return {"read": read_n, "merged": read_n, "marked": marked}


def forward_etl_overrides(conn) -> dict:
    """Promote unprocessed segment overrides → gold, then mark them processed."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT customer_id, segment_id, actor_email, updated_at "
            "FROM customer_segment_overrides_staging WHERE processed = false "
            "ORDER BY customer_id"
        )
        rows = cur.fetchall()

    read_n = len(rows)
    if read_n == 0:
        return {"read": 0, "merged": 0, "marked": 0}

    now = datetime.now(timezone.utc)
    df = spark.createDataFrame(
        [Row(customer_id=r[0], segment_id=r[1], actor_email=r[2], updated_at=r[3]) for r in rows],
        schema=OVERRIDES_SCHEMA,
    ).withColumn("processed_at", F.lit(now).cast("timestamp"))
    df.createOrReplaceTempView("_overrides_updates")

    spark.sql(
        f"""
        MERGE INTO {GOLD_OVERRIDES} t
        USING _overrides_updates s ON t.customer_id = s.customer_id
        WHEN MATCHED THEN UPDATE SET
            t.segment_id  = s.segment_id,
            t.actor_email = s.actor_email,
            t.updated_at  = s.updated_at,
            t.processed_at = s.processed_at
        WHEN NOT MATCHED THEN INSERT
            (customer_id, segment_id, actor_email, updated_at, processed_at)
            VALUES (s.customer_id, s.segment_id, s.actor_email, s.updated_at, s.processed_at)
        """
    )

    # Mark processed VERSION-AWARE-LY. customer_segment_overrides_staging is an
    # UPSERT table keyed on customer_id, so the app can overwrite a customer's
    # row with a NEWER value (new updated_at, processed reset to false) BETWEEN
    # our SELECT above and this UPDATE. A key-only UPDATE would then mark that
    # newer, never-merged version processed=true and it would silently never
    # reach gold. So we scope the UPDATE to the EXACT (customer_id, updated_at)
    # we read+merged, and require processed=false: if the app wrote a newer
    # override in between, its updated_at differs and its row is NOT marked —
    # the next run picks it up and merges the newer value. (Notes are keyed on
    # the IDENTITY `id`, unique per insert and never overwritten, so they have
    # no such exposure and mark by id alone is safe.)
    pairs = [(r[0], r[3]) for r in rows]  # (customer_id, updated_at) actually merged
    marked = 0
    with conn.cursor() as cur:
        for customer_id, updated_at in pairs:
            cur.execute(
                "UPDATE customer_segment_overrides_staging SET processed = true "
                "WHERE customer_id = %s AND updated_at = %s AND processed = false",
                (customer_id, updated_at),
            )
            marked += cur.rowcount
    return {"read": read_n, "merged": read_n, "marked": marked}


# COMMAND ----------

# MAGIC %md
# MAGIC ## 5 · Run + summary

# COMMAND ----------

with lakebase_connect(autocommit=True) as conn:
    notes_stats = forward_etl_notes(conn)
    overrides_stats = forward_etl_overrides(conn)

notes_total = spark.table(GOLD_NOTES).count()
overrides_total = spark.table(GOLD_OVERRIDES).count()

summary = {
    "notes": notes_stats,
    "overrides": overrides_stats,
    "gold_notes_rowcount": notes_total,
    "gold_overrides_rowcount": overrides_total,
}

print("=== Forward-ETL summary ===")
print(f"notes     : read={notes_stats['read']} merged={notes_stats['merged']} "
      f"marked_processed={notes_stats['marked']}")
print(f"overrides : read={overrides_stats['read']} merged={overrides_stats['merged']} "
      f"marked_processed={overrides_stats['marked']}")
print(f"gold {GOLD_NOTES} rowcount     = {notes_total}")
print(f"gold {GOLD_OVERRIDES} rowcount = {overrides_total}")

# Surface the summary to the Jobs API (run output) so the app can display it.
dbutils.notebook.exit(str(summary))
