# T7 — Forward ETL (Pattern A): psycopg + MERGE INTO Delta

**Pull-based, on-demand.** Promotes app-written Lakebase *staging* rows into UC
**gold** Delta tables. Triggered on demand by the Streamlit app's Reports page
("Run forward-ETL") via the Jobs API — no streaming, no always-on compute.

## Flow

```
                         ┌─────────────────────────────────────────┐
 Reports page  ──run_now──▶  capstone-forward-etl  (serverless job) │
 (app SP, jobs.py)         │                                         │
                          │  per staging table:                     │
                          │   1. SELECT ... WHERE processed = false │  psycopg (SP OAuth token)
                          │   2. MERGE INTO <gold> ON <pk>          │  Spark / Delta
                          │   3. UPDATE *_staging SET processed=true│  psycopg
                          │      WHERE <pk> = ANY(<merged ids>)     │
                          └─────────────────────────────────────────┘
```

1. **Connect** to Lakebase Postgres via **psycopg** as the job's run-as
   identity — the **app service principal** once bound at T8; the current user
   in the sandbox until then. A fresh ~1h Databricks OAuth token is minted per
   run via the SDK and used as the Postgres password over `sslmode=require`
   (same pattern as `app/lib/db.py` / `lakebase/reverse_etl/_common.py`). The
   token is never stored or logged.
2. **Read** only unconsumed rows: `SELECT ... WHERE processed = false`.
3. **MERGE** the rows into the gold Delta target on the table's primary key
   (`WHEN MATCHED UPDATE` / `WHEN NOT MATCHED INSERT`), so re-runs converge to
   the same result.
4. **Mark processed** — only after the MERGE commits — for exactly the rows
   merged. **Notes** are marked by their IDENTITY `id` (unique per insert, never
   overwritten, so key-only is safe). **Overrides** are marked **version-aware**:
   `UPDATE ... SET processed = true WHERE customer_id = %s AND updated_at = %s
   AND processed = false` for each `(customer_id, updated_at)` actually merged
   (see "Version-safety" below).
5. **Print a summary** (read / merged / marked counts + gold rowcounts) and
   surface it via `dbutils.notebook.exit(...)` so the Jobs API run output
   carries it back to the app.

### Ordering / crash safety

The Delta MERGE runs **before** the staging `UPDATE`. If the job dies in
between, gold is updated but staging is still `processed = false` — the next run
simply re-merges those rows (MERGE on the PK is idempotent) and then marks them
processed. So a partial failure never loses data and never double-writes, and
the whole job is safe to re-run. The `processed = false` filter + idempotent
MERGE together make re-running with no new rows a **no-op**.

### Version-safety (upsert-keyed overrides)

`customer_segment_overrides_staging` is an **UPSERT** table keyed on
`customer_id`: the app overwrites a customer's single row on each override
(bumping `updated_at`, resetting `processed = false`). That creates a race the
notes table does not have — between the ETL's `SELECT` and its mark-processed
`UPDATE`, the app can overwrite that customer's row with a **newer** value.
`max_concurrent_runs = 1` stops overlapping ETL jobs but **not** concurrent app
writes.

A key-only `UPDATE ... WHERE customer_id = ...` would then mark the *newer*,
never-merged version `processed = true`, and it would silently never reach gold
(**data loss**). To prevent this, the mark step matches the **full payload** we
actually read+merged — for each `(customer_id, updated_at, segment_id,
actor_email)` we run

```sql
UPDATE customer_segment_overrides_staging
SET processed = true
WHERE customer_id = %s AND updated_at = %s AND segment_id = %s
  AND actor_email = %s AND processed = false
```

(parameterized — every value is bound, never f-strung).

**Why the full payload, not `updated_at` alone.** `updated_at` DEFAULTs to
`now()`, which in Postgres is the **transaction-start** time with finite
precision — it is *not* guaranteed unique per upsert, so two rapid upserts for
the same customer can share an identical `updated_at`. Matching on
`updated_at` alone could therefore still mark a newer, never-merged row. By also
comparing `segment_id` and `actor_email`, the predicate matches **0 rows**
whenever the app wrote *any* different value in between (different segment,
different actor, or a newer `updated_at`) — the newer row stays
`processed = false` and the next run merges it into gold. (If a coincident write
happened to have the identical payload *and* identical `updated_at`, the value
is byte-for-byte what gold already holds, so marking it processed loses
nothing.)

**Gold ↔ mark consistency.** The `MERGE INTO` above writes exactly
`(customer_id, segment_id, actor_email, updated_at)` from the rows we read, and
the mark predicate matches that same tuple — so gold and the mark can never
disagree about which version was materialised.

**Notes** are marked by their IDENTITY `id` (unique per insert, never
overwritten), so a key-only mark is already safe there and is left as-is.

## Gold target mapping

| Staging table | Gold Delta target | MERGE key | Why |
|---|---|---|---|
| `customer_notes_staging` | `<catalog>.<schema>.customer_notes` | `id` (staging IDENTITY PK) | Notes are append-style facts; the staging PK is the natural stable key. |
| `customer_segment_overrides_staging` | `<catalog>.<schema>.customer_segment_overrides` | `customer_id` | One effective override per customer (staging PK is `customer_id`). |

Segment overrides materialise into a **dedicated gold override table** rather
than mutating `customers.segment`, because `customers` is kept fresh from gold
by the T1 reverse-ETL sync — writing to it from forward-ETL would fight that
sync. Analytics can `LEFT JOIN customer_segment_overrides` onto `customers` to
compute the effective segment. In this sandbox `<catalog>.<schema>` =
`suro_cat.app_capstone`.

## The job

`create_job.py` uploads `forward_etl.py` to the workspace (under `PARENT_PATH`)
and creates/updates a **serverless** job named `capstone-forward-etl` that runs
it. Idempotent (matched by job name). It prints the `job_id`, which is the
`FORWARD_ETL_JOB_ID` that:

* `app/lib/jobs.py` triggers (`run_now`) and polls (`get_run`), and
* the bundle binds to the app **at T8** via `resources/app.yml`'s
  `forward-etl-job` resource → `app.yaml` `valueFrom`.

> **Created job:** `capstone-forward-etl`, `job_id=139812673079227`
> (workspace `fe-sandbox-suro-serverless-sandbox-24july`). Must be wired into
> `resources/app.yml` + `app.yaml` `FORWARD_ETL_JOB_ID` at T8.

All operational values (catalog, schema, instance, database, host) are **job
parameters** with sandbox defaults (documented in `forward_etl.py`'s widgets);
override them per run via the Jobs API for another workspace.

## Run-as identity note

Until the app is deployed (T8), no app service principal exists yet, so the job
runs as the creating user (`surojit.chowdhury@databricks.com`), who owns both
`suro_cat` (gold) and `capstone-pg` (Lakebase) — so the MERGE and the psycopg
connection both succeed without extra grants. At T8 the job's run-as becomes the
app SP; the SP already has `SELECT, INSERT, UPDATE` on the staging tables (T1
grants) and needs `MODIFY` on the two gold targets (grant at deploy time).

## Usage

```bash
# from repo root; env comes from app/.env
uv run lakebase/forward_etl/pattern_a_psycopg2/create_job.py   # create/update the job, prints job_id
```

Then trigger it from the app's **Reports** page, or via the Jobs API / CLI.

## Files

| File | What it does |
|---|---|
| `forward_etl.py` | The Databricks notebook (source format): read `processed=false` → MERGE gold → mark processed → print summary. |
| `create_job.py` | Uploads the notebook + creates/updates the serverless `capstone-forward-etl` job (idempotent). Prints `FORWARD_ETL_JOB_ID`. |

## Verified run evidence

* Seeded 2 notes into `customer_notes_staging` (ids **4, 5**;
  customers `C0000000`, `C0000001`; `processed=false`).
* Triggered the job — run **944972956185035** → **SUCCESS**;
  output `notes: read=2 merged=2 marked=2`, `gold_notes_rowcount=2`.
* Confirmed both notes present in `suro_cat.app_capstone.customer_notes` and
  both staging rows now `processed=true`.
* No-op re-run **535712601630897** → **SUCCESS**;
  output `notes: read=0 merged=0 marked=0`, `gold_notes_rowcount=2` (unchanged).
