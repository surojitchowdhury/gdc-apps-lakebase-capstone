"""Typed data-access layer for the Customer 360 app.

This module is the single seam between the Streamlit pages and the two backing
data systems, and it keeps the **auth boundary** explicit:

* **Lakebase (Postgres) via the app service principal** — all customer reads
  (``customers_synced`` / ``transactions_synced``) and all writes
  (notes / segment overrides / audit log). Uses :func:`app.lib.db.lakebase_sp`.
* **SQL warehouse via OBO (the calling user)** — cross-table gold aggregates in
  :func:`get_customer_metrics`, run through :mod:`app.lib.sql` with the OBO
  :class:`WorkspaceClient` so the query is attributed to the real user in the
  SQL audit log.

Deliberately **free of Streamlit imports** so it can be exercised headlessly and
unit-tested. Every user-supplied value is bound as a query parameter — psycopg
``%s`` for Lakebase, ``:name`` markers for the warehouse — never f-string
interpolated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from databricks.sdk import WorkspaceClient

from . import sql as wsql
from .db import lakebase_sp

# Hard cap on rows returned per page. The customer list can be ~10k rows; we
# NEVER read them all — the UI pages through server-side LIMIT/OFFSET and this
# cap bounds any single response regardless of what the caller requests.
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25

# Synced tables (reverse-ETL from gold) live in Postgres schema ``public``.
_CUSTOMERS_TABLE = "customers_synced"
_TRANSACTIONS_TABLE = "transactions_synced"

# Most recent transactions surfaced on the detail page.
_RECENT_TXN_LIMIT = 20


# --- Typed return shapes ----------------------------------------------------
@dataclass
class Page:
    """A single server-side page of customer rows.

    ``total`` is the count of rows matching the *filters* (not the page), so the
    UI can render "page X of N" and disable Next on the last page.
    """

    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        """Total number of pages for the current filter + page_size."""
        if self.page_size <= 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
        }


@dataclass
class Customer:
    """A customer profile plus their most-recent transactions (detail view)."""

    profile: "dict[str, Any] | None"
    transactions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"profile": self.profile, "transactions": self.transactions}


# --- Reads (Lakebase synced tables, via app SP) -----------------------------
def _clamp_page_size(page_size: int) -> int:
    """Bound page_size to ``[1, MAX_PAGE_SIZE]`` so a caller can never pull all
    rows in one query."""
    if page_size is None or page_size < 1:
        return DEFAULT_PAGE_SIZE
    return min(page_size, MAX_PAGE_SIZE)


def list_customers(
    segment: "str | None" = None,
    min_ltv: "float | None" = None,
    max_churn: "float | None" = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Page:
    """Server-side paginated + filtered read of ``customers_synced`` (app SP).

    Filtering and pagination both happen in Postgres: we build a parameterized
    ``WHERE`` from whichever filters are provided, ``COUNT(*)`` the matches for
    ``total``, then fetch exactly one page with ``LIMIT/OFFSET``. We never
    ``SELECT`` the full table.

    Parameters
    ----------
    segment:
        Exact ``segment_id`` to match (``None`` = any).
    min_ltv:
        Minimum ``lifetime_value`` inclusive (``None`` = no lower bound).
    max_churn:
        Maximum ``churn_score`` inclusive (``None`` = no upper bound).
    page:
        1-based page number.
    page_size:
        Rows per page, clamped to ``MAX_PAGE_SIZE``.
    """
    page = max(1, int(page))
    page_size = _clamp_page_size(int(page_size))
    offset = (page - 1) * page_size

    # Build a parameterized WHERE. Each user value is a %s placeholder; the
    # matching value goes into `params` positionally — never interpolated.
    clauses: list[str] = []
    params: list[Any] = []
    if segment:
        clauses.append("segment_id = %s")
        params.append(segment)
    if min_ltv is not None:
        clauses.append("lifetime_value >= %s")
        params.append(min_ltv)
    if max_churn is not None:
        clauses.append("churn_score <= %s")
        params.append(max_churn)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    count_sql = f"SELECT COUNT(*) AS n FROM {_CUSTOMERS_TABLE} {where}"
    page_sql = (
        "SELECT customer_id, first_name, last_name, email, country, city, "
        "segment_id, lifetime_value, churn_score, last_purchase_date "
        f"FROM {_CUSTOMERS_TABLE} {where} "
        "ORDER BY lifetime_value DESC NULLS LAST, customer_id "
        "LIMIT %s OFFSET %s"
    )

    with lakebase_sp() as conn, conn.cursor() as cur:
        cur.execute(count_sql, params)
        total = int(cur.fetchone()[0])

        cur.execute(page_sql, [*params, page_size, offset])
        columns = [c.name for c in cur.description]
        items = [dict(zip(columns, row)) for row in cur.fetchall()]

    return Page(items=items, total=total, page=page, page_size=page_size)


def get_customer(customer_id: str) -> Customer:
    """Profile from ``customers_synced`` + the last 20 rows of
    ``transactions_synced`` for this customer (app SP, parameterized).

    Returns a :class:`Customer` whose ``profile`` is ``None`` when the id is not
    found (the transactions list is then empty).
    """
    profile_sql = f"SELECT * FROM {_CUSTOMERS_TABLE} WHERE customer_id = %s"
    txn_sql = (
        "SELECT transaction_id, product_id, transaction_date, channel, status, amount "
        f"FROM {_TRANSACTIONS_TABLE} WHERE customer_id = %s "
        "ORDER BY transaction_date DESC, transaction_id DESC "
        "LIMIT %s"
    )

    with lakebase_sp() as conn, conn.cursor() as cur:
        cur.execute(profile_sql, [customer_id])
        row = cur.fetchone()
        if row is None:
            return Customer(profile=None, transactions=[])
        prof_cols = [c.name for c in cur.description]
        profile = dict(zip(prof_cols, row))

        cur.execute(txn_sql, [customer_id, _RECENT_TXN_LIMIT])
        txn_cols = [c.name for c in cur.description]
        transactions = [dict(zip(txn_cols, r)) for r in cur.fetchall()]

    return Customer(profile=profile, transactions=transactions)


def list_notes(customer_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return a customer's notes from ``customer_notes_staging`` (app SP).

    Most-recent first, capped at ``limit`` rows. Parameterized; used by the
    Notes tab to show existing notes (including one just added, after the cache
    is invalidated).
    """
    limit = max(1, min(int(limit), 200))
    notes_sql = (
        "SELECT id, customer_id, note, actor_email, created_at, processed "
        "FROM customer_notes_staging WHERE customer_id = %s "
        "ORDER BY created_at DESC, id DESC LIMIT %s"
    )
    with lakebase_sp() as conn, conn.cursor() as cur:
        cur.execute(notes_sql, [customer_id, limit])
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# --- Metrics (gold cross-table aggregates, via SQL warehouse + OBO) ---------
def _gold(table: str) -> str:
    """Fully-qualified gold table name from env (CAPSTONE_CATALOG/SCHEMA).

    Identifiers come from trusted config, not user input, so interpolating them
    into the FROM clause is safe; the ``customer_id`` filter is still bound as a
    ``:customer_id`` parameter.
    """
    catalog = os.environ.get("CAPSTONE_CATALOG")
    schema = os.environ.get("CAPSTONE_SCHEMA")
    if not catalog or not schema:
        raise RuntimeError(
            "CAPSTONE_CATALOG / CAPSTONE_SCHEMA are not set (check app/.env)."
        )
    return f"{catalog}.{schema}.{table}"


def get_customer_metrics(customer_id: str, obo: WorkspaceClient) -> dict[str, Any]:
    """Cross-table gold aggregates for a customer, run as the **calling user**.

    Runs against gold (``CAPSTONE_CATALOG.CAPSTONE_SCHEMA.*``) through the SQL
    warehouse using the OBO :class:`WorkspaceClient` passed in, so the query is
    attributed to the real user in the SQL audit log. The customer id is a bound
    ``:customer_id`` parameter.

    Joins ``transactions`` to ``products`` to compute spend, transaction volume,
    recency, distinct products/categories, and top spend channel + category.
    Returns a single-row dict of aggregates (zero-filled when the customer has no
    transactions in gold).

    Numeric aggregates are returned as real Python ``float`` / ``int`` — NOT the
    raw strings the API hands back. The Statement Execution API returns every
    ``data_array`` value as a STRING (even numbers), so we coerce at this
    boundary; callers (e.g. the Metrics tab's ``:,.2f`` formatting) get real
    numbers and never hit a ``ValueError`` on a string. Date columns are left as
    their string form; ``top_channel`` / ``top_category`` are strings by nature.

    Raises ``ValueError`` if ``obo`` is ``None`` — the caller (the Metrics tab)
    must surface the OBO-not-enabled message instead of falling back to the SP.
    """
    if obo is None:
        raise ValueError(
            "OBO WorkspaceClient is required for get_customer_metrics: gold "
            "metrics must run as the calling user, never the service principal."
        )

    txns = _gold("transactions")
    products = _gold("products")

    metrics_sql = f"""
        SELECT
            COUNT(*)                                   AS transaction_count,
            COALESCE(SUM(t.amount), 0)                 AS total_spend,
            COALESCE(AVG(t.amount), 0)                 AS avg_transaction_amount,
            COALESCE(MAX(t.amount), 0)                 AS max_transaction_amount,
            COUNT(DISTINCT t.product_id)               AS distinct_products,
            COUNT(DISTINCT p.category)                 AS distinct_categories,
            MAX(t.transaction_date)                    AS last_transaction_date,
            MIN(t.transaction_date)                    AS first_transaction_date,
            SUM(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS completed_count
        FROM {txns} t
        LEFT JOIN {products} p ON t.product_id = p.product_id
        WHERE t.customer_id = :customer_id
    """

    top_channel_sql = f"""
        SELECT t.channel AS channel, COALESCE(SUM(t.amount), 0) AS channel_spend
        FROM {txns} t
        WHERE t.customer_id = :customer_id
        GROUP BY t.channel
        ORDER BY channel_spend DESC
        LIMIT 1
    """

    top_category_sql = f"""
        SELECT p.category AS category, COALESCE(SUM(t.amount), 0) AS category_spend
        FROM {txns} t
        LEFT JOIN {products} p ON t.product_id = p.product_id
        WHERE t.customer_id = :customer_id
        GROUP BY p.category
        ORDER BY category_spend DESC
        LIMIT 1
    """

    params = {"customer_id": customer_id}
    metrics_rows = wsql.query(obo, metrics_sql, params)
    channel_rows = wsql.query(obo, top_channel_sql, params)
    category_rows = wsql.query(obo, top_category_sql, params)

    raw: dict[str, Any] = metrics_rows[0] if metrics_rows else {}

    # Coerce the string-valued aggregates the Statement Execution API returns
    # into real numbers so the UI can format them directly. Integer-valued
    # counts -> int; money/averages -> float. Missing/None -> 0 (a customer with
    # zero transactions still yields a well-formed all-zero metrics dict).
    int_fields = (
        "transaction_count",
        "distinct_products",
        "distinct_categories",
        "completed_count",
    )
    float_fields = (
        "total_spend",
        "avg_transaction_amount",
        "max_transaction_amount",
    )
    metrics: dict[str, Any] = {}
    for f in int_fields:
        metrics[f] = _as_int(raw.get(f))
    for f in float_fields:
        metrics[f] = _as_float(raw.get(f))
    # Date columns stay as their string representation (or None).
    metrics["last_transaction_date"] = raw.get("last_transaction_date")
    metrics["first_transaction_date"] = raw.get("first_transaction_date")

    metrics["top_channel"] = channel_rows[0]["channel"] if channel_rows else None
    metrics["top_channel_spend"] = (
        _as_float(channel_rows[0]["channel_spend"]) if channel_rows else None
    )
    metrics["top_category"] = category_rows[0]["category"] if category_rows else None
    metrics["top_category_spend"] = (
        _as_float(category_rows[0]["category_spend"]) if category_rows else None
    )
    return metrics


def _as_float(value: Any) -> float:
    """Coerce a Statement-Execution string (or None/number) to ``float``.

    The API returns numeric cells as strings; ``None`` / empty -> ``0.0``."""
    if value is None or value == "":
        return 0.0
    return float(value)


def _as_int(value: Any) -> int:
    """Coerce a Statement-Execution string (or None/number) to ``int``.

    Parses via ``float`` first so values like ``"11.0"`` are handled; ``None`` /
    empty -> ``0``."""
    if value is None or value == "":
        return 0
    return int(float(value))


# --- Writes (Lakebase staging + audit, single transaction, via app SP) ------
def _placeholder_actor(actor_email: "str | None") -> str:
    """Actor recorded in staging/audit rows. Falls back to a sentinel when the
    ``X-Forwarded-Email`` header is absent (headless / pre-OBO)."""
    return actor_email or "unknown@local"


def add_note(customer_id: str, body: str, actor_email: "str | None") -> dict[str, Any]:
    """Insert a customer note AND append an audit-log row in ONE transaction.

    Both writes commit together or not at all: we open a single non-autocommit
    connection, execute both INSERTs, then commit; any error rolls back both so
    a note can never land without its matching audit row (and vice-versa).

    Returns ``{"note_id": ..., "audit_id": ...}`` for the two inserted rows.
    All values are bound as psycopg ``%s`` parameters.
    """
    actor = _placeholder_actor(actor_email)

    insert_note = (
        "INSERT INTO customer_notes_staging (customer_id, note, actor_email) "
        "VALUES (%s, %s, %s) RETURNING id"
    )
    insert_audit = (
        "INSERT INTO customer_audit_log (actor_email, action, customer_id, detail) "
        "VALUES (%s, %s, %s, %s::jsonb) RETURNING id"
    )

    # autocommit=False => the two INSERTs are one atomic unit.
    with lakebase_sp(autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(insert_note, [customer_id, body, actor])
                note_id = cur.fetchone()[0]

                detail = _json_detail({"note_id": note_id, "note_preview": body[:200]})
                cur.execute(
                    insert_audit,
                    [actor, "add_note", customer_id, detail],
                )
                audit_id = cur.fetchone()[0]
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {"note_id": note_id, "audit_id": audit_id}


def override_segment(
    customer_id: str, segment_id: str, actor_email: "str | None"
) -> dict[str, Any]:
    """UPSERT a segment override AND append an audit row in ONE transaction.

    The override table's PK is ``customer_id`` and we
    ``ON CONFLICT (customer_id) DO UPDATE``, so re-submitting the SAME value for
    a customer updates the single existing row rather than inserting a duplicate
    — the override write is **idempotent** (exactly one override row per
    customer, ever). A fresh audit row IS appended on every submission, on
    purpose: the audit log is an append-only trail of actions taken, so
    re-submitting the same value is still a recorded action.

    Returns ``{"customer_id": ..., "segment_id": ..., "audit_id": ...}``.
    """
    actor = _placeholder_actor(actor_email)

    upsert_override = (
        "INSERT INTO customer_segment_overrides_staging "
        "  (customer_id, segment_id, actor_email, processed) "
        "VALUES (%s, %s, %s, false) "
        "ON CONFLICT (customer_id) DO UPDATE SET "
        "  segment_id = EXCLUDED.segment_id, "
        "  actor_email = EXCLUDED.actor_email, "
        "  updated_at = now(), "
        "  processed = false "
        "RETURNING customer_id, segment_id"
    )
    insert_audit = (
        "INSERT INTO customer_audit_log (actor_email, action, customer_id, detail) "
        "VALUES (%s, %s, %s, %s::jsonb) RETURNING id"
    )

    with lakebase_sp(autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(upsert_override, [customer_id, segment_id, actor])
                cust, seg = cur.fetchone()

                detail = _json_detail({"segment_id": segment_id})
                cur.execute(
                    insert_audit,
                    [actor, "override_segment", customer_id, detail],
                )
                audit_id = cur.fetchone()[0]
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return {"customer_id": cust, "segment_id": seg, "audit_id": audit_id}


def _json_detail(obj: dict[str, Any]) -> str:
    """Serialize an audit ``detail`` payload to a JSON string for the ``jsonb``
    column. Local import keeps the module's import surface minimal."""
    import json

    return json.dumps(obj)
