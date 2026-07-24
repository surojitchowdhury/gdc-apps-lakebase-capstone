# Capstone — Customer 360 on Databricks Apps + Lakebase (Streamlit edition)

> This is the **Streamlit** variant of the capstone. It builds the exact
> same Customer 360 app as `CAPSTONE_TASKS.md`, but the UI + serving layer
> is a single **Streamlit** app instead of a React frontend + FastAPI
> backend. Tasks **T3** (app code + UI) and the **Optimizations** section
> are rewritten for Streamlit; the data, auth, Lakebase, Genie, dashboard,
> forward-ETL, and deployment tasks are otherwise identical.

## What you're building

A "customer success" web app for **Acme Retail** (a synthetic 10k-customer
retail dataset, already provisioned in your workspace by the installer).
Reps use the app to:

- Browse customer accounts (list with filters: segment, LTV, churn risk)
- Open a 360° view (profile + last 20 transactions + computed metrics)
- Leave **notes** and override **segments** (writes go to Lakebase staging). Merge these back to delta for analytics.
- Ask **Genie** ad-hoc questions
- View an embedded **AI/BI dashboard**
- Trigger a **forward-ETL** job that promotes staging rows into gold

A separate `/api/external/*` surface (defined in **T3a**) exposes the
same data to partner systems via **M2M** (service-principal
client_credentials → OAuth access token). Partners send
`Authorization: Bearer <token>` to the Apps proxy; the proxy validates
and forwards `X-Forwarded-Access-Token` to the handler. The handler
reads from **Delta gold via the SQL warehouse using the caller's bearer
(OBO)** — never falls back to Lakebase, never to the app SP — so warehouse
RLS / audit reflect the caller's identity.

> **Streamlit note on the external surface:** Streamlit does not expose
> custom HTTP routes, so the `/api/external/*` M2M surface cannot live
> inside the Streamlit process. T3a is implemented as a small, separate
> FastAPI app (its own Databricks App). See **T3a** for the deployment
> shape — the auth model and data path are unchanged.

---

## User journey

The app is for **customer success reps** who want to understand and act on
customer insights without leaving the tool. A typical session:

1. **Sign in** — automatic via OBO (the Databricks Apps proxy injects
   the user's identity); no login screen of your own.
2. **Customer list** (Customers page) — the default landing page. Rep
   filters by segment, minimum LTV, maximum churn risk; selects a row to
   drill in.
3. **Customer detail** — tabbed view (`st.tabs`):
   - **Profile** — name, contact, segment, signup date, churn score
   - **Metrics** — lifetime spend, top-5 categories, last-30 / 90-day
     totals, open ticket count, avg CSAT (computed live via SQL warehouse
     aggregation across multiple gold tables)
   - **Activity** — last 20 transactions
   - **Notes** — list existing notes + form to add a new one
   - **Segment override** — current segment + form to override
4. **Genie** (Genie page / chat element) — chat box that answers ad-hoc
   questions ("Top 5 segments by LTV last quarter", "Which customers in EU
   have churn > 0.7?"). Surface it as a persistent chat panel reachable
   from the sidebar.
5. **Dashboard** (Dashboard page) — embedded AI/BI dashboard for broader
   analytics (segment LTV, top products, ticket trends, churn histogram).
6. **Reports** (Reports page) — "Run forward-ETL" button + run-status
   indicator + history of recent runs.

---

## App design & UI requirements

Reviewers will judge the app on polish, update the below UI elements as per your design sense.
- **Tech Stack:**
  - App: **Streamlit**, Databricks SDK, psycopg, uv, Python 3.11
  - Single Python process; no separate frontend/backend, no Node build step
- **Layout:** use **Streamlit multipage** (`st.navigation` + `st.Page`, or
  a `pages/` directory). Persistent left sidebar nav (Customers, Dashboard,
  Reports, Genie), a header showing the signed-in user's email and a
  workspace badge (`st.context.headers`), content in the main area. Use
  `st.set_page_config(layout="wide")` and a custom theme via
  `.streamlit/config.toml`.
- **Vibe Assistance:** Leverage databricks ai-tools assistance like vibe, isaac, cursor, ai-dev-kit for developing the solution. Use [go/vibe](https://go/vibe) or [go/aidevkit](https://go/aidevkit) to install.

---

## What this capstone tests

Every skill from the Apps + Lakebase training:

- OBO + service-principal authentication
- Lakebase reverse ETL (synced tables) and writable staging tables
- Lakebase CRUD with audit, transactional safety
- SQL warehouse query from an App
- Genie Conversation API
- Lakeview dashboard embed
- `app.yaml` env + secrets binding + OBO scopes
- M2M authentication for external API surface
- Forward ETL (staging → gold)
- DABs + **git-source** app deployment via local `bundle deploy` / `bundle run`
- Lakebase ops: branching, PITR, query insights

The repo-root **`README.md`** documents the `curl … | bash` installer
that has already provisioned: gold tables, Lakebase instance, AI/BI
dashboard, Genie space, and your `app/.env`. From here on out you write
the app.

## Prerequisites

- Databricks workspace access (UC enabled; can create Lakebase + apps).
- A Serverless SQL warehouse you can use (the installer let you pick one).
- `databricks` CLI ≥ 0.299, `uv`. (No `node` required for the Streamlit
  app; only T3a's FastAPI sidecar is Python too.)
- Forked this scaffold into your own repo (private is fine) — required
  for **T8** (git-source app deployment).

---

## Provisioned gold tables

The installer creates **5 Delta tables** in `<CAPSTONE_CATALOG>.gold`
(catalog name is in your `app/.env`). Schemas you'll write SQL / psycopg
against:

### `customers` — 10,000 rows
| column | type |
|---|---|
| `customer_id` | string (PK, e.g. `C0003600`) |
| `first_name`, `last_name`, `email`, `phone` | string |
| `country`, `city`, `gender` | string |
| `age` | int |
| `signup_date`, `last_purchase_date` | date |
| `segment_id` | string (FK → `customer_segments`) |
| `lifetime_value` | double |
| `churn_score` | double (0–1) |
| `updated_at` | timestamp |

### `transactions` — ~100k rows
| column | type |
|---|---|
| `transaction_id` | string (PK) |
| `customer_id` | string (FK → `customers`) |
| `product_id` | string (FK → `products`) |
| `transaction_date` | date |
| `channel` | string (`web`, `mobile`, `store`, …) |
| `status` | string (`completed`, `pending`, `cancelled`) |
| `amount` | double |

### `products` — 200 rows
| column | type |
|---|---|
| `product_id` | string (PK) |
| `name`, `category`, `subcategory`, `brand` | string |
| `price` | double |
| `in_stock` | boolean |

### `customer_segments` — 7 rows
| column | type |
|---|---|
| `segment_id` | string (PK, `S1`–`S7`) |
| `segment_name` | string (Champions, Loyal, At Risk, Potential Loyalists, Hibernating, …) |
| `description`, `criteria` | string |

### `support_tickets`
| column | type |
|---|---|
| `ticket_id` | string (PK) |
| `customer_id` | string (FK → `customers`) |
| `category`, `priority`, `status`, `channel` | string |
| `subject` | string |
| `opened_at`, `closed_at` | date |
| `csat_score` | int (1–5) |

> **Mapping into Lakebase (T1):** `customers`, `transactions`, and
> `products` get synced tables (`customers_synced`, …). `support_tickets`
> and `customer_segments` stay in gold and are queried via the SQL
> warehouse — that's why the **Metrics** path takes the warehouse
> route (it joins `transactions` × `products` × `support_tickets`).

---

## T1 — Reverse ETL: synced + staging tables

**Why this is needed:** Your app needs sub-10ms customer reads (Lakebase
*synced* tables, kept fresh from gold) AND a place to write notes /
segment overrides without touching gold (Lakebase *staging* tables).
This task wires both.

**Do this:**

- Create 3 Lakebase synced tables in **CONTINUOUS** mode (so writes to
  gold appear in Lakebase within seconds — required for the app to
  reflect upstream changes live):
  - `customers_synced` ← `<catalog>.gold.customers` (CONTINUOUS)
  - `transactions_synced` ← `<catalog>.gold.transactions` (CONTINUOUS)
  - `products_synced` ← `<catalog>.gold.products` (TRIGGERED hourly,
    because the catalog is slow-changing — justify this choice in your
    submission reflection)
- Create 3 writable staging tables in Lakebase via psycopg DDL:
  - `customer_notes_staging` (with `processed BOOLEAN DEFAULT false`)
  - `customer_segment_overrides_staging` (same)
  - `customer_audit_log` (append-only)

**Guidance (saves real pain):**

- **App SP needs explicit grants** to read synced tables and read/write
  staging tables — fresh PG roles have no privileges. Run a one-time grant
  step (after the app SP has logged in to Lakebase at least once) that
  GRANTs SELECT on synced + SELECT/INSERT/UPDATE on staging + USAGE on
  sequences to the SP role (the role name is the SP's `client_id` UUID).
  Add an `ALTER DEFAULT PRIVILEGES` so future syncs inherit access.

**Docs:**
- Synced tables: https://docs.databricks.com/aws/en/oltp/projects/sync-tables
- Lakebase Postgres connection: https://docs.databricks.com/aws/en/oltp/projects/external-apps-connect


**Done when:**
- [ ] All 3 synced tables show **CONTINUOUS** state in the Lakebase UI
- [ ] All 3 staging tables exist (`\dt` via psycopg) with the right columns

---

## T2 — Auth: OBO and service-principal clients

**Why:** Every SQL warehouse / Genie call needs an identity. **OBO**
carries the calling user's identity through the app to data services
(so workspace-level RLS and audit work). **SP** is for app-level work
that isn't tied to a user (Lakebase access, background jobs, cron).

**Do this:** in `app/lib/auth.py`, implement:

- `obo_client() -> WorkspaceClient` — read `X-Forwarded-Access-Token`
  from `st.context.headers` and build a `WorkspaceClient(token=...)`.
  Used for SQL warehouse + Genie. (In Streamlit there is no per-call
  `request` object — the inbound request headers for the current session
  are exposed via `st.context.headers`.)
- `sp_client() -> WorkspaceClient` — module-level client using the
  app's service-principal credentials (provided by the runtime). Used
  for **all Lakebase access** and for the forward-ETL job trigger.
- `current_user_email() -> str` — read `X-Forwarded-Email` from
  `st.context.headers` (used as the actor for the audit log).

In `app/lib/db.py`, implement a single psycopg connection helper
`lakebase_sp()` that mints a fresh OAuth token (Lakebase Postgres tokens
expire ~1h, re-mint per checkout, or pool with token rotation). **Do not
write a `lakebase_obo()` — Lakebase doesn't yet support OBO scopes**, so
calling `generate_database_credential` with a user OBO bearer fails with
`Provided OAuth token does not have required scopes: postgres`. All
in-app DB reads/writes run as the SP; record the calling user from
`X-Forwarded-Email` for the audit log.

**Guidance (saves real pain):**

- **Enable the OBO preview toggle on the workspace.** Workspace admin →
  Settings → Apps → **User authorization (preview)**. Without it,
  `user_api_scopes` PATCH calls return 200 but the field is silently
  purged, and `X-Forwarded-Access-Token` never gets injected.
- **Use only platform-allowed scopes.** This capstone uses exactly
  `sql` (warehouse) and `dashboards.genie` (Genie API).
- **First app load triggers a consent screen.** Each user must click
  "Authorize" once for the listed scopes before `X-Forwarded-Access-Token`
  flows. Admins can pre-grant on behalf of users.
- **`st.context.headers` reflects the current inbound request** — read it
  inside the script run, don't cache the token across reruns past its TTL.

**Docs:**
- OBO + scopes: https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth
- HTTP headers passed to apps: https://docs.databricks.com/aws/en/dev-tools/databricks-apps/http-headers

**Cookbook:** https://apps-cookbook.dev/docs/streamlit/authentication/users_get_current

**Done when:**
- [ ] A debug widget that calls `obo_client().current_user.me()` shows the *calling user* (not the SP)
- [ ] A call using `sp_client()` runs as the service principal in audit logs
- [ ] `SELECT 1` against Lakebase via `lakebase_sp()` works

---

## T3 — App data layer + Streamlit UI

**Why:** These operations exercise every read/write pattern the training
covers — Lakebase synced reads, SQL warehouse for cross-table aggregates,
Lakebase staging writes with audit. In a Streamlit app there is no HTTP
API between a frontend and backend; instead you build a clean **data-access
layer** (plain Python functions) that the pages call directly, keeping the
same auth boundaries (Lakebase via **app SP**, metrics via **SQL warehouse
+ OBO**).

### Data-access functions (`app/lib/data.py`)

Implement these as typed functions returning plain dicts / dataclasses /
DataFrames. Keep SQL parameterized; never f-string user input.

| Group | Function | What it does | Skill |
|---|---|---|---|
| **Reads** | `list_customers(segment, min_ltv, max_churn, page, page_size) -> Page` | Paginated list from `customers_synced` (Lakebase via **app SP**). Server-side pagination + filtering; returns `{items, total, page, page_size}`. | Lakebase synced reads |
| | `get_customer(customer_id) -> dict` | Profile from `customers_synced` + last 20 from `transactions_synced` (Lakebase via **app SP**). | Lakebase synced reads |
| | `get_customer_metrics(customer_id, obo) -> dict` | Cross-table aggregates against gold via the **SQL warehouse with OBO** (calling user's bearer). | SQL warehouse + OBO |
| **Writes** (transactional + audited) | `add_note(customer_id, body, actor_email)` | INSERT into `customer_notes_staging` AND append to `customer_audit_log` in the **same transaction** (Lakebase via **app SP**, actor email from `X-Forwarded-Email`). | Lakebase CRUD + audit |
| | `override_segment(customer_id, segment_id, actor_email)` | UPSERT into `customer_segment_overrides_staging` AND append to `customer_audit_log` in the same transaction (idempotent). | Lakebase CRUD + audit |

> Keep the data layer free of Streamlit imports where practical so it stays
> testable. Pagination, parameterization, and the single-transaction
> write+audit invariant matter just as much here as in the FastAPI version.

### Streamlit UI

| Page | Functions used | Notes |
|---|---|---|
| `pages/1_Customers.py` | `list_customers` | Filter widgets (segment `selectbox`, min-LTV / max-churn `slider`/`number_input`) + `st.dataframe`. Server-side pagination via page controls (`st.session_state["page"]`) — **never** load 10k rows. Row selection (`st.dataframe(selection_mode=...)` or a "View" button per row) sets the selected `customer_id` in `st.session_state` and navigates to detail (`st.switch_page`). |
| `pages/2_Customer_Detail.py` | `get_customer`, `get_customer_metrics`, `add_note`, `override_segment` | `st.tabs(["Profile", "Activity", "Notes", "Segment"])`. Run the Metrics warehouse query only when its tab is active (or behind `@st.fragment`) so the expensive OBO query doesn't block first paint. Note + segment forms use `st.form` so a single submit triggers one write; `st.cache_data.clear()` or a scoped cache key bump re-fetches after a write. |

**Files:**
- App entry + nav: `app/streamlit_app.py`
- Data layer: `app/lib/data.py`, `app/lib/db.py`, `app/lib/auth.py`,
  `app/lib/sql.py` (warehouse `statement_execution` helper)
- Pages: `app/pages/1_Customers.py`, `app/pages/2_Customer_Detail.py`

**Docs:**
- SQL Statement Execution: https://docs.databricks.com/aws/en/dev-tools/sql-execution-tutorial
- Lakebase from Apps: https://docs.databricks.com/aws/en/oltp/projects/databricks-apps
- Apps HTTP headers (`X-Forwarded-Access-Token`): https://docs.databricks.com/aws/en/dev-tools/databricks-apps/http-headers
- Streamlit multipage apps: https://docs.streamlit.io/develop/concepts/multipage-apps
- Streamlit caching: https://docs.streamlit.io/develop/concepts/architecture/caching

**Cookbook:**
- SQL warehouse + tables: https://apps-cookbook.dev/docs/streamlit/tables/tables_edit
- Auth recipes: https://apps-cookbook.dev/docs/streamlit/authentication/users_get_current

**Done when:**
- [ ] All data-layer functions return the correct shape, exercised through the Streamlit UI
- [ ] Customer list paginates server-side (page-size cap enforced; never reads all 10k rows in one query)
- [ ] Adding a note appears in the list immediately (cache invalidated) AND a row exists in `customer_audit_log` for every write
- [ ] Overriding a segment is idempotent (re-submitting the same value is a no-op, not a duplicate row)
- [ ] The Metrics tab's warehouse query runs as the calling user (OBO), confirmed in the SQL audit log

---

## T4 — Embed the AI/BI dashboard

**Why:** Reps want broader analytics in-app without leaving for the
workspace UI. iframe embed is the supported integration pattern.

**Do this:**

- Read `DATABRICKS_HOST` and `DASHBOARD_ID` from env (no `/api/config`
  endpoint needed — Streamlit reads its own config directly).
- In `pages/3_Dashboard.py`, render the embed with
  `st.components.v1.iframe(f"{host}/embed/dashboardsv3/{dashboard_id}", height=900)`
  (or `components.html` with an `<iframe>` if you need more control).

**Guidance:**
- **Allowlist your app's domain in the workspace.** Workspace Settings →
  Security → External Access → **Embed Dashboard** → add your app's host
  (e.g. `customer360-<workspace>.azure.databricksapps.com`). Without this
  the iframe is blocked by `X-Frame-Options` and the dashboard never
  renders.

**Files:** `app/pages/3_Dashboard.py`, `app/lib/config.py`

**Docs:** https://www.databricks.com/blog/how-embed-aibi-dashboards-your-websites-and-applications

**Done when:**
- [ ] Dashboard renders inside the app and displays data (no "blocked by
      X-Frame-Options" or auth errors in the browser console)

---

## T5 — Integrate Genie chat

**Why:** Reps want to ask ad-hoc questions ("which segments saw
declining LTV in Q3?") in plain English. Genie's conversation API drives
the chat UX.

**Do this:** in `app/lib/genie.py`, build three OBO-backed helpers:

- `start_conversation(...)` → `genie.start_conversation`
- `create_message(conversation_id, content)` → `genie.create_message`
- `get_message(conversation_id, message_id)` → `genie.get_message`
  (poll until status terminal; if it has an attachment, fetch the
  attachment query result)

Build the chat UI with native Streamlit chat primitives:

- A **Genie page** (`pages/4_Genie.py`) using `st.chat_input` +
  `st.chat_message`, with conversation history kept in
  `st.session_state` (so follow-ups stay in the same Genie conversation).
- Show a typing indicator while polling (`st.spinner` / `st.status`),
  cap polls at ~30s, and surface a friendly error if the message never
  reaches a terminal state.
- Render result attachments as `st.dataframe`, and add an "Open in
  workspace" link that deep-links to the Genie space.

> Streamlit has no true floating overlay; the supported pattern is a
> dedicated chat page (or a `st.sidebar` chat element) reachable from the
> nav. Use `st.session_state` to persist `conversation_id` + message list
> across reruns.

**Files:** `app/lib/genie.py`, `app/pages/4_Genie.py`

**Docs:** https://docs.databricks.com/aws/en/genie/conversation-api

**Cookbook:** https://apps-cookbook.dev/docs/streamlit/bi/genie_api

**Done when:**
- [ ] "Top segment by LTV" returns an answer + a result preview
- [ ] Follow-up questions in the same conversation maintain context

---

## T6 — App configuration: `app.yaml`

**Why:** `app.yaml` is the single config that ties the deployed app to
the resources you provisioned. Without it: missing secrets at runtime,
OBO scope mismatches, and Lakebase auth failure. Key blocks:

- `command` — start Streamlit:
  `["streamlit", "run", "streamlit_app.py", "--server.port", "8000", "--server.address", "0.0.0.0"]`
  (Databricks Apps inject the port; match it to `$DATABRICKS_APP_PORT` if
  required by your runtime).
- `env` — wire static + dynamic env vars: `PGHOST`, `PGDATABASE`,
  `WAREHOUSE_ID`, `DASHBOARD_ID`, `GENIE_SPACE_ID`, `PARENT_PATH`,
  `PG_UC_CATALOG`, etc. (read these from your `app/.env`). Bundle-injected
  values (e.g. `FORWARD_ETL_JOB_ID`) come via `valueFrom` referencing the
  resource name declared in `resources/app.yml`.
- `user_authorization` (OBO scopes) — list **only**: `sql` and
  `dashboards.genie`. The platform auto-adds `iam.current-user:read` and
  `iam.access-control:read` as defaults. Other scopes (`dashboards`,
  `iam.access-control:read` listed explicitly, `postgres`) are rejected
  by the Apps API.


**Guidance:**
- **OBO requires the workspace preview toggle to be ON** (see T2).
  Without it, scopes won't persist on the deployed app and
  `X-Forwarded-Access-Token` is never injected.
- **First load of the app prompts each user for consent** on the listed
  scopes — they must click Authorize once before OBO carries through.

**Files:** `app/app.yaml`

**Docs:**
- App runtime config: https://docs.databricks.com/aws/en/dev-tools/databricks-apps/app-runtime
- Env vars + secrets binding: https://docs.databricks.com/aws/en/dev-tools/databricks-apps/environment-variables
- Resources binding: https://docs.databricks.com/aws/en/dev-tools/databricks-apps/resources
- OBO scopes: https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth

**Done when:**
- [ ] App starts (Streamlit boots) with no missing-secret errors
- [ ] `obo_client()` can call SQL warehouse and Genie, and `sp_client()` can reach Lakebase, without 401s

---

## T7 — Forward ETL: staging → gold

**Why:** Notes and overrides the app writes go into Lakebase staging.
To materialise them into Delta gold (for analytics, ML, audit) you need
a forward-ETL flow that propagates staging rows into gold. Two
architectures are accepted — pull-based and batched (Pattern A) or
push-based and CDC-streamed (Pattern B) — and the "Run forward-ETL"
button on your Reports page triggers the relevant compute in each.

**Do this — pick ONE pattern:**

- **Pattern A — psycopg + MERGE INTO Delta (pull, on-demand):**
  Notebook job in `lakebase/forward_etl/pattern_a_psycopg2/`. Connect
  to Lakebase via psycopg as the SP, read `*_staging WHERE processed=false`,
  build a Spark DataFrame, `MERGE INTO gold.customer_notes ON ...`, then
  `UPDATE *_staging SET processed=true WHERE id IN (...)` in the same
  transaction. The Reports button triggers this job directly via the
  Jobs API.

- **Pattern B — [Lakehouse Sync](https://docs.databricks.com/aws/en/oltp/projects/lakehouse-sync) (native Lakebase CDC, Beta):**
  Use Lakebase's built-in Lakehouse Sync to continuously replicate the
  staging tables into UC-managed Delta tables (`lb_<table>_history`) as
  **SCD Type 2** — every insert / update / delete is appended as a new
  row with `_change_type`, `_timestamp`, `_lsn`, `_xid` system columns.
  Replication itself needs **no external compute, pipeline, or job**;
  it's a native Lakebase feature powered by the `wal2delta` Postgres
  extension.


Then wire the job into the app (same surface for both patterns):

- `run_forward_etl()` (SP client) — triggers the job
  (Pattern A: the MERGE job; Pattern B: the dedup-into-gold job)
- `get_run(run_id)` — polls run status
- `pages/5_Reports.py` — "Run forward-ETL" button (`st.button`) +
  status indicator (`st.status` / progress + auto-refresh via
  `st.rerun` or `@st.fragment(run_every=...)`) + a recent-runs table.

**Files:** `lakebase/forward_etl/...`, `app/lib/jobs.py`,
`app/pages/5_Reports.py`

**Docs:**
- Lakehouse Sync (Pattern B reference): https://docs.databricks.com/aws/en/oltp/projects/lakehouse-sync
- Lakebase + Apps integration: https://docs.databricks.com/aws/en/oltp/projects/databricks-apps

**Done when:**
- [ ] Triggering the job from the Reports page produces a successful run
- [ ] Re-running with no new staging rows is a no-op (Pattern A:
      `processed=false` filter; Pattern B: dedup CTAS/MERGE is
      naturally idempotent)
- [ ] `gold.customer_notes` rowcount equals the expected unique-note
      count in staging (Pattern A: rows with `processed=true`;
      Pattern B: distinct PKs surviving dedup of `lb_*_history`)

---

## T8 — Deploy via DABs as a git-source app

**Why:** The production pattern for Apps is **git-source apps** declared
via DABs. The DABs `app` resource declares the GitHub repo + branch and
Databricks pulls the source from there each `bundle run`. **For this
capstone the deployed app must be a git-source app** — source-code-path-
only apps that upload a workspace folder are explicitly **not** accepted.

**Deploy path (run locally — no GitHub Actions required):**

```
databricks bundle validate --target prod --profile <profile>
databricks bundle deploy   --target prod --profile <profile>
databricks bundle run customer360 --target prod --profile <profile>
```

`bundle run` is what makes Databricks pull the latest commit from the
declared git ref and restart the app — it is **not** a job-trigger. Run
it locally after every `bundle deploy`. CI is intentionally out of scope
for this capstone; the inner-loop is `git push` + the three commands
above.

**Do this:**

- `databricks.yml` — bundle root with `targets: dev / prod`, project
  name, default workspace host, and `variables:` for `warehouse_id`,
  `lakebase_instance`, `dashboard_id`, `genie_space_id`, `catalog`,
  `pg_uc_catalog`, `git_repo_url`, `git_branch`.
- `resources/app.yml` — define the app(s) as **git-source app(s)**. Set
  `git_repository.provider: github` + `git_repository.url`, plus
  `git_source.branch` + `git_source.source_code_path` (path inside the
  repo — e.g. `app/` for the Streamlit app). **Do not also set
  `source_code_path` at the app level** — DABs rejects "both git_source
  and source_code_path are set". Declare app resources block
  (`sql_warehouse`, `database`, `genie_space`, the forward-ETL `job`) and
  `user_api_scopes: [sql, dashboards.genie]`. If you deploy the T3a
  external FastAPI app, declare it as a **second** git-source app pointing
  at `external_api/`.
  > Requires Databricks CLI ≥ 0.290.0 for `git_repository` / `git_source`
  > on app resources.
- `resources/jobs.yml` — define the forward-ETL job from T7.
- `resources/lakebase.yml` — declarative synced-table specs (the YAML
  equivalent of T1's psycopg DDL), so synced tables are part of the
  bundle and don't drift from manual creation.

**Guidance for a private git repo (most common case):**

- **The app's service principal must own the git credential** — the
  workspace pulls source as the SP, not as the deploying user. The
  `principal_id` field on `git-credentials create` binds the credential
  to the SP in **one call**, run as your normal user profile — no SP
  impersonation, no SP client_secret, no extra CLI profile needed:
  1. After the first `bundle deploy`, get the app's
     `service_principal_id` from `databricks apps get <name>`.
  2. Register the GitHub credential bound to that SP id:
     ```
     databricks git-credentials create --json '{
       "git_provider": "gitHub",
       "git_email": "<bot-email>",
       "personal_access_token": "<github_pat>",
       "principal_id": <APP_SP_ID>,
       "name": "GitHub credentials for app SP"
     }' --profile <your-profile>
     ```
  3. Re-run `databricks bundle run <app-name> --target prod` — source
     pull should now succeed.
- If you delete and re-create the app, the `service_principal_id`
  changes — re-register the git credential against the new SP id. The
  CLI's "default" git credential set against your user account does
  **not** apply to apps.

**Streamlit-specific notes (no Node build):**

- **No `package.json`, no `npm build`, no `dist/` to commit.** The
  Streamlit app ships as Python source; the runtime command is just
  `streamlit run streamlit_app.py` (see T6). This sidesteps the React
  build gotchas entirely.
- **Pin dependencies** in `requirements.txt` (or `pyproject.toml` + `uv`)
  at the app source root so the Apps runtime installs them. Include
  `streamlit`, `databricks-sdk`, `psycopg[binary]`, `psycopg-pool`.
- Keep the Streamlit app and the T3a FastAPI app in **separate source
  directories** so each app's dependency set and start command stay clean.

**Files:** `databricks.yml`, `resources/app.yml`, `resources/jobs.yml`,
`resources/lakebase.yml`

**Docs:**
- DABs for Apps tutorial: https://docs.databricks.com/aws/en/dev-tools/bundles/apps-tutorial
- DABs Apps resource reference (incl. `git_repository` / `git_source`): https://docs.databricks.com/aws/en/dev-tools/bundles/resources#app
- Git-source apps overview: https://docs.databricks.com/aws/en/dev-tools/databricks-apps/git

**Done when:**
- [ ] `databricks bundle validate --target prod` passes
- [ ] In the workspace UI, the deployed app's source shows the **git
      repository + branch** (not a workspace folder upload)
- [ ] `databricks bundle run customer360 --target prod` pulls the
      latest commit and the app's Deployments tab shows the matching
      commit SHA

---

## T9 — Lakebase ops

| # | Task | What to do | Skill |
|---|---|---|---|
| **T9a** | Branch + PITR | Create a child branch from `capstone-pg`. On the branch, `DELETE FROM customer_notes_staging` (destructive). On the parent, restore to a timestamp before the delete. Capture screenshots of branch creation and the post-restore row count. | Branching + PITR |
| **T9b** | Query insights | Run `SELECT … WHERE actor_email = '…'` against `customer_audit_log` 100×. Open Query Performance (or `pg_stat_statements`) — the query is slow because there's no index. `CREATE INDEX ON customer_audit_log (actor_email)`. Re-run; record before/after p95 latency. | Query perf |

**Docs:**
- Branches: https://docs.databricks.com/aws/en/oltp/projects/branches
- PITR: https://docs.databricks.com/aws/en/oltp/projects/point-in-time-restore
- Query Performance UI: https://docs.databricks.com/aws/en/oltp/projects/query-performance
- pg_stat_statements: https://docs.databricks.com/aws/en/oltp/projects/pg-stat-statements

**Done when:**
- [ ] Screenshots of branch creation, PITR restore, and before/after p95 latency

---

## Optimizations & engineering hygiene

Reviewers will look for a real production-grade **Streamlit** app, not a
demo script. Address these patterns explicitly — call them out in your
submission writeup.

### Pagination (server-side, always)

- `list_customers` accepts `page` + `page_size` (or a cursor) and returns
  `{ items, total, page, page_size }`. Never load 10k rows into a
  DataFrame. Drive page state with `st.session_state` and Prev/Next
  buttons (or `st.number_input` for page).
- Default `page_size = 25`, hard cap at `100`. Reject/clamp larger values.
- Add a Lakebase index on the columns you sort/filter by (e.g. composite
  on `segment_id, lifetime_value DESC`); without it `OFFSET` over a
  large dataset gets slow fast.
- Prefer **keyset pagination** (`WHERE lifetime_value < :last_seen ...
  ORDER BY lifetime_value DESC LIMIT 25`) over `OFFSET` once the dataset
  grows beyond a few thousand rows.

### Caching (Streamlit-native)

- **Data caching:** wrap read functions in **`@st.cache_data`** with a
  `ttl`. Suggested defaults:
  - Segments list / products list / config: `ttl=300` (5 min) — rarely change
  - Customer list page: `ttl=10`
  - Customer detail: `ttl=30`
  - Customer metrics (expensive OBO warehouse query): `ttl=60`
  Key the cache on the actual arguments (filters, page, `customer_id`).
  **Do not cache writes**, and after a write call `st.cache_data.clear()`
  (or bump a per-customer cache key) so the UI re-fetches.
- **Resource caching:** the Lakebase connection pool and the
  `WorkspaceClient` SP client belong in **`@st.cache_resource`** (one
  shared instance per server process), **not** `@st.cache_data`.
- **OBO token caution:** the per-user OBO bearer comes from
  `st.context.headers` each run — don't bake it into a long-lived
  `@st.cache_data` entry past its ~1h TTL. Cache the *result* of an OBO
  query briefly, not the token.
- **Browser:** Streamlit serves a SPA; you don't hand-set `Cache-Control`,
  but you control re-compute cost via the cache TTLs above.

### Connection pooling (Lakebase)

- Use `psycopg_pool.ConnectionPool` (size 2–10), created once inside an
  `@st.cache_resource` function so all reruns/sessions in a worker share it.
  Without pooling you pay TLS + auth on every interaction.
- Lakebase OAuth tokens expire (~1h). Either (a) set the pool's
  `reconnect_failed=True` and supply a fresh token via a `configure`/
  connection callback on every checkout, or (b) recreate the pool on token
  refresh. Either is fine; document which you chose.

### Streamlit performance

- **Minimize work per rerun.** Every widget interaction reruns the whole
  script — guard expensive calls behind `@st.cache_data` and behind the
  active tab (only run the Metrics warehouse query when its tab is open).
- **Use `@st.fragment`** to scope reruns: e.g. the Notes form, the Reports
  run-status poller (`@st.fragment(run_every=2)`), and filter widgets
  should rerun only their fragment, not the whole page.
- **Persist navigation + selection state in `st.session_state`**
  (selected `customer_id`, current page, Genie `conversation_id`) so
  interactions don't lose context across reruns.
- **Don't fan out reads serially when avoidable.** The detail page should
  load Profile + Activity from one Lakebase round-trip (single query or a
  pooled batch) and defer Metrics to its tab — avoid an N+1 pattern of one
  query per field.
- **Debounce-like behavior:** wrap filter inputs in an `st.form` (submit
  to apply) so typing doesn't trigger a query on every keystroke.

### Data-access hygiene

- **Parameterize all SQL** (psycopg `%(name)s` / SQL Statement Execution
  parameters) — never f-string user input into queries.
- Return the minimum columns — don't `SELECT *` if the UI needs 6 fields.
- Use typed return shapes (dataclasses / `TypedDict` / Pydantic models)
  for the data layer so the contract is explicit and testable.
- Set sensible timeouts on outbound calls (warehouse, Lakebase, Genie)
  so a slow downstream doesn't tie up the Streamlit worker; surface
  failures with `st.error` rather than a raw traceback.

### Observability

- Structured logging (`logging.getLogger(__name__)` + JSON formatter).
- Generate/propagate a per-interaction request id (store in
  `st.session_state`) and log it across the Streamlit → Lakebase / SQL hop
  for correlation.
- Log slow queries (Lakebase / SQL warehouse) with their parameters at
  `WARNING` level when they exceed a threshold (e.g. 500ms).

**Done when:**
- [ ] Customer list serves any page in < 200ms server-side
      (cold cache, warehouse not involved).
- [ ] Detail page renders to first paint in < 800ms with cache warm
      (Metrics tab deferred).
- [ ] Cache hits are observable on tab switches and back-navigation
      (e.g. log a cache-miss marker; misses don't recur for warm keys).
- [ ] No N+1 Lakebase queries on the detail page (verify in logs).
- [ ] Writeup explicitly calls out the caching (`st.cache_data` /
      `st.cache_resource` TTLs), pagination, and pooling choices you made.

---

## Submission

- [ ] Every task above checked
- [ ] Repo URL (public is fine — see T8 for SP-bound git credential)
- [ ] Live app URL (deployed as a **Streamlit git-source app** via local
      `databricks bundle deploy` + `bundle run`)
- [ ] 3-min screen recording: customer list → detail (all tabs) → add
      note → override segment → genie → dashboard → run forward-ETL
- [ ] Output from `examples/m2m_test.py` (T3a) pasted in your writeup,
      showing the M2M flow returns `200` + customer JSON.
- [ ] T9 screenshots (branch + PITR, before/after p95 latency)
- [ ] One-paragraph reflection: which sync mode you chose for each
      synced table and why, plus which optimizations you implemented
      and which you'd add next

## Skills coverage map

| Skill | Tested by |
|---|---|
| Lakebase synced tables (sync mode choice) | T1 + reflection |
| Lakebase psycopg + DDL | T1, T3 (notes / override writes), T6 (env wiring) |
| Lakebase synced reads | T3 (List + Detail) |
| Lakebase CRUD + audit | T3 (notes + segment override) |
| OBO + SP authentication | T2 |
| OAuth scopes + `user_authorization` | T6 |
| SQL warehouse from an App | T3 (Metrics) |
| External M2M auth + warehouse OBO | T3a |
| Lakeview dashboard embed | T4 |
| Genie Conversation API | T5 |
| Forward ETL | T7 |
| DABs + git-source app (local deploy/run) | T8 |
| Lakebase branching, PITR, query perf | T9 |
| Streamlit app engineering (caching, pagination, pooling, theming) | App design + Optimizations |
