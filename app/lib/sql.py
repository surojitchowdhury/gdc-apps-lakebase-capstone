"""SQL warehouse helper for the Customer 360 app.

Runs **parameterized** queries against the SQL warehouse (``WAREHOUSE_ID``) via
the Databricks *Statement Execution* API, using a :class:`WorkspaceClient`
supplied by the caller. In this app the caller passes the **OBO** client
(:func:`app.lib.auth.obo_client`) so the statement runs as the *calling user*
and shows up in the SQL audit log under their identity — this is the only place
the app touches gold data on the user's behalf.

Why the caller passes the client (rather than this module building one): the
auth boundary is a deliberate design point of the capstone. Lakebase work runs
as the app service principal; gold/warehouse work runs as the user (OBO). By
taking the client as an argument this helper stays agnostic and the boundary is
visible at every call site.

Parameterization: user-supplied values are ALWAYS bound as named
:class:`StatementParameterListItem` markers (``:name`` in the SQL) — never
f-string-interpolated. This is the SQL-warehouse analogue of psycopg's ``%s``
binding used on the Lakebase side.
"""

from __future__ import annotations

import os
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementParameterListItem, StatementState


def _warehouse_id() -> str:
    """SQL warehouse id from the environment (``WAREHOUSE_ID``, via app/.env)."""
    wid = os.environ.get("WAREHOUSE_ID")
    if not wid:
        raise RuntimeError(
            "WAREHOUSE_ID is not set. In production it is provided by app.yaml; "
            "locally it is read from app/.env."
        )
    return wid


def _to_params(params: "dict[str, Any] | None") -> "list[StatementParameterListItem] | None":
    """Convert a ``{name: value}`` mapping into Statement Execution parameters.

    Values are passed as strings (the API's default parameter type is STRING and
    it casts server-side against the column type). ``None`` maps to a NULL-valued
    parameter. Returns ``None`` when there are no parameters.
    """
    if not params:
        return None
    items: list[StatementParameterListItem] = []
    for name, value in params.items():
        items.append(
            StatementParameterListItem(
                name=name,
                value=None if value is None else str(value),
            )
        )
    return items


def query(
    client: WorkspaceClient,
    sql: str,
    params: "dict[str, Any] | None" = None,
    *,
    warehouse_id: "str | None" = None,
    wait_timeout: str = "50s",
) -> "list[dict[str, Any]]":
    """Run a parameterized ``SELECT`` and return rows as a list of plain dicts.

    Parameters
    ----------
    client:
        The :class:`WorkspaceClient` to run *as*. Callers pass the OBO client so
        the statement is attributed to the calling user in the SQL audit log.
    sql:
        SQL text with ``:name`` markers for every user-supplied value.
    params:
        ``{name: value}`` bound to the ``:name`` markers. Never f-string user
        input into ``sql`` — pass it here.

    Returns a list of ``{column_name: value}`` dicts (empty list for no rows).
    Raises :class:`RuntimeError` if the statement does not finish SUCCEEDED.
    """
    resp = client.statement_execution.execute_statement(
        warehouse_id=warehouse_id or _warehouse_id(),
        statement=sql,
        parameters=_to_params(params),
        wait_timeout=wait_timeout,
        # Cap payload defensively — callers should aggregate/limit in SQL, not
        # pull large result sets back into the app process.
        row_limit=10_000,
    )

    state = resp.status.state if resp.status else None
    if state != StatementState.SUCCEEDED:
        err = resp.status.error if resp.status and resp.status.error else None
        detail = f": {err.message}" if err else ""
        raise RuntimeError(
            f"SQL statement did not succeed (state={state.value if state else 'UNKNOWN'})"
            f"{detail}"
        )

    return _rows_as_dicts(resp)


def _rows_as_dicts(resp: Any) -> "list[dict[str, Any]]":
    """Zip the inline result's column names with each row's values."""
    result = resp.result
    manifest = resp.manifest
    if not result or not result.data_array:
        return []
    columns = (
        [c.name for c in manifest.schema.columns]
        if manifest and manifest.schema and manifest.schema.columns
        else []
    )
    return [dict(zip(columns, row)) for row in result.data_array]
