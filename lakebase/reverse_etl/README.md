# T1 — Reverse ETL: Lakebase synced + staging tables

This directory wires the two halves of the app's Lakebase data layer:

1. **Synced tables** (reverse ETL) — read-optimized copies of the UC gold
   Delta tables, kept fresh in Lakebase Postgres so the app gets sub-10ms
   customer reads.
2. **Staging tables** — writable Postgres tables the app uses to record notes,
   segment overrides, and an audit trail **without touching read-only gold**.

Everything is driven by config from `app/.env` (loaded by `_common.py`); no host
names, catalog names, or secrets are hard-coded. The connection config
(`PG_INSTANCE_NAME`, `PGDATABASE`, `PG_UC_CATALOG`, plus `PGHOST`,
`CAPSTONE_CATALOG`, `CAPSTONE_SCHEMA`, `DATABRICKS_PROFILE`) is **required from
the environment** — there are no committed operational defaults, so a missing
value fails fast rather than silently targeting the wrong instance. Lakebase
connections use a short-lived (~1h) Databricks OAuth token as the Postgres
password — minted per run via the SDK, never stored or logged.

## Actual resources used (serverless sandbox)

| Thing | Value |
|---|---|
| CLI profile | `fe-sandbox-24july` |
| Lakebase instance | `capstone-pg` (CU_1, PG, AVAILABLE) |
| Postgres database | `capstone_db` |
| **UC database catalog** | **`suro_capstone_lb_sbx`** — owned by the user, bound to `capstone-pg` / `capstone_db` |
| Gold source tables | `suro_cat.app_capstone.{customers,transactions,products}` |
| Secret scope | `capstone-surojit-chowdhury` |

> **Catalog note:** synced tables land in `suro_capstone_lb_sbx.public.<name>`
> in UC, which surfaces in Postgres under database `capstone_db`, schema
> `public`. Do **not** use `capstone_lakebase` — that is a *different user's*
> catalog on another workspace and raises `Cross workspace access is not
> allowed`.

## Synced tables

| Synced table | Source (gold) | PK | Sync mode |
|---|---|---|---|
| `customers_synced` | `suro_cat.app_capstone.customers` | `customer_id` | **CONTINUOUS** |
| `transactions_synced` | `suro_cat.app_capstone.transactions` | `transaction_id` | **CONTINUOUS** |
| `products_synced` | `suro_cat.app_capstone.products` | `product_id` | **TRIGGERED** (hourly) |

All three gold sources have Change Data Feed enabled
(`delta.enableChangeDataFeed = true`), which CONTINUOUS mode requires to stream
incremental changes.

### CONTINUOUS vs TRIGGERED — cost / freshness tradeoff

A synced table is backed by a Lakeflow (DLT) pipeline that copies gold → Postgres.
The **scheduling policy** decides when that pipeline runs, which is the core
cost-vs-freshness lever:

- **CONTINUOUS** — the pipeline runs **always-on** and streams changes as they
  land in gold, so the Lakebase copy is fresh within seconds (~15s floor).
  You pay for continuously-running compute whether or not the source changes.
  Use it when the app must reflect upstream changes live.
  - `customers_synced` — churn scores, lifetime value, and last-purchase dates
    change frequently and the app surfaces them in real time.
  - `transactions_synced` — a live activity feed; new transactions must appear
    immediately.

- **TRIGGERED** — the pipeline runs **only when invoked**, spins compute up for
  the refresh, then shuts it down. Much cheaper for slow-changing data, at the
  cost of staleness bounded by the trigger cadence.
  - `products_synced` — the product catalog is small and slow-changing (price /
    stock edits are infrequent), so an always-on pipeline would waste money. We
    refresh it **hourly**, which is well within the freshness this dimension
    needs. The hourly cadence is enforced by a Databricks Job
    (`capstone-products_synced-hourly-refresh`) with a quartz-cron trigger
    (`0 0 * * * ?`) that kicks the underlying pipeline.

**Rule of thumb:** CONTINUOUS = pay-for-freshness (real-time, always-on
compute); TRIGGERED = pay-for-what-you-use (bounded staleness, cheap for
slow-changing tables). Match the mode to how fast the data actually changes and
how live the app needs it.

## Staging tables (Postgres-only, writable)

Created via idempotent psycopg DDL in `capstone_db`, schema `public`:

| Table | Purpose | Notes |
|---|---|---|
| `customer_notes_staging` | Free-text agent notes on a customer | `processed BOOLEAN DEFAULT false` lets forward-ETL (T7) pick up unhandled rows |
| `customer_segment_overrides_staging` | Manual segment reassignment | PK on `customer_id` → UPSERT makes overrides idempotent; `processed` flag |
| `customer_audit_log` | Append-only trail of every mutating action | `detail JSONB` for structured, queryable context |

These are **not** synced from gold — the app writes to them directly as the
service principal. T7 (forward ETL) promotes their rows back into gold.

## Grants for the app service principal

Fresh Lakebase PG roles have **no** privileges on synced/staging tables. After
the app service principal has logged into Lakebase at least once (its PG role
name is the SP's `client_id` UUID), run the grant script to give it:

- `SELECT` on the 3 synced tables (read-only),
- `SELECT, INSERT, UPDATE` on the 3 staging tables (no `DELETE` — the audit log
  is append-only and staging rows are marked `processed`, not removed),
- `USAGE, SELECT` on sequences (for `IDENTITY` columns),
- `ALTER DEFAULT PRIVILEGES` so **future** synced tables/sequences in `public`
  inherit access automatically.

The live run is **guarded on role existence**: if the SP role doesn't exist yet
(the app hasn't been deployed / hasn't logged in), the script prints a SKIPPED
notice and exits 0 — safe to run unconditionally in automation.

## Scripts

| Script | What it does |
|---|---|
| `_common.py` | Shared config + Lakebase connection helper (OAuth token as password). |
| `create_synced_tables.py` | Creates the 3 synced tables (idempotent), polls until each reaches a healthy sync state, and creates/updates the hourly refresh Job for `products_synced`. **Fails loud:** exits non-zero if any synced table enters a terminal error state (exit 1) or the poll times out before all are healthy (exit 4); the required `products_synced` hourly refresh Job is guaranteed — it polls for the underlying pipeline id and exits non-zero (exit 5) rather than skipping the Job if it can't be obtained. |
| `create_staging_tables.py` | Applies idempotent staging DDL and verifies columns via `information_schema`. |
| `grant_app_sp.py` | Renders (`--dry-run`) or applies the SP grants; live run is guarded on role existence. |

### Usage

```bash
# from repo root; env comes from app/.env

# 1. Synced tables (creates + waits for healthy; --recreate to rebuild, --no-wait to skip polling)
uv run lakebase/reverse_etl/create_synced_tables.py

# 2. Staging tables (idempotent)
uv run lakebase/reverse_etl/create_staging_tables.py

# 3. Grants — after the app SP's first Lakebase login
uv run lakebase/reverse_etl/grant_app_sp.py --dry-run --sp-role <sp-client-id-uuid>   # preview SQL
uv run lakebase/reverse_etl/grant_app_sp.py --sp-role <sp-client-id-uuid>             # apply (guarded)
```

## Docs

- Synced tables: https://docs.databricks.com/aws/en/oltp/projects/sync-tables
- Lakebase Postgres connection: https://docs.databricks.com/aws/en/oltp/projects/external-apps-connect
