"""Databricks Jobs helpers for the Customer 360 app (forward-ETL trigger).

The forward-ETL job (T7, Pattern A) promotes Lakebase staging rows into UC gold
Delta tables. The Reports page triggers it on demand and polls its status.

**Auth boundary.** Forward-ETL is *app-level* work (not scoped to an individual
user), so it runs as the app **service principal** — we use
:func:`app.lib.auth.sp_client`, mirroring Lakebase access. The calling user's
identity is not needed to move staging → gold.

**Job id.** Read from ``FORWARD_ETL_JOB_ID`` (env, injected by the bundle at T8
via ``app.yaml`` ``valueFrom`` → the ``forward-etl-job`` app resource). Until
that binding exists the var is unset and :func:`run_forward_etl` raises a clear
error — the Reports page surfaces it as a warning rather than crashing.

Deliberately **Streamlit-free** so it stays importable and unit-testable from
the CLI / headless contexts.
"""

from __future__ import annotations

import os
from typing import Any

from .auth import sp_client


def forward_etl_job_id() -> "str | None":
    """Return ``FORWARD_ETL_JOB_ID`` from the environment, or ``None`` if unset.

    Unset is an EXPECTED pre-T8 state (the bundle binds this var when the app is
    deployed as a git-source app). Callers should show a friendly warning rather
    than treating ``None`` as an error.
    """
    value = os.environ.get("FORWARD_ETL_JOB_ID")
    return value or None


def _require_job_id() -> int:
    """Return the forward-ETL job id as an int, raising a clear error if unset."""
    raw = forward_etl_job_id()
    if not raw:
        raise RuntimeError(
            "FORWARD_ETL_JOB_ID is not set. It is bound by the bundle at deploy "
            "time (T8) via app.yaml valueFrom → the forward-etl-job app resource. "
            "Set it in app/.env to trigger the job locally."
        )
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise RuntimeError(
            f"FORWARD_ETL_JOB_ID must be an integer job id, got {raw!r}."
        ) from exc


def run_forward_etl(job_params: "dict[str, str] | None" = None) -> int:
    """Trigger the forward-ETL job as the app **service principal** (`run_now`).

    Parameters
    ----------
    job_params:
        Optional ``{name: value}`` notebook parameters to override the job's
        defaults (e.g. ``catalog`` / ``schema`` / ``pg_instance``). Passed through
        as ``notebook_params`` — values must be strings.

    Returns the numeric ``run_id`` of the triggered run. Poll it with
    :func:`get_run`.
    """
    w = sp_client()
    resp = w.jobs.run_now(
        job_id=_require_job_id(),
        notebook_params=job_params or None,
    )
    return resp.run_id


def get_run(run_id: int) -> dict[str, Any]:
    """Return a run's status via the SP client, as a plain dict.

    Keys: ``run_id``, ``life_cycle_state`` (PENDING/RUNNING/TERMINATED/…),
    ``result_state`` (SUCCESS/FAILED/…, ``None`` until terminal),
    ``state_message``, ``run_page_url``, and ``is_terminal`` (True once the run
    has finished, however it finished).
    """
    w = sp_client()
    run = w.jobs.get_run(run_id=run_id)

    state = run.state
    life_cycle = state.life_cycle_state if state else None
    result = state.result_state if state else None

    # A run is terminal once its life-cycle state is TERMINATED / SKIPPED /
    # INTERNAL_ERROR — at which point result_state is meaningful.
    terminal_life_cycles = {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}
    life_cycle_str = life_cycle.value if life_cycle else None

    return {
        "run_id": run_id,
        "life_cycle_state": life_cycle_str,
        "result_state": result.value if result else None,
        "state_message": state.state_message if state else None,
        "run_page_url": run.run_page_url,
        "is_terminal": life_cycle_str in terminal_life_cycles,
    }


def list_recent_runs(limit: int = 10) -> list[dict[str, Any]]:
    """Return recent runs of the forward-ETL job (most recent first).

    Each entry: ``run_id``, ``life_cycle_state``, ``result_state``,
    ``start_time`` (epoch ms), ``run_page_url``. Returns ``[]`` if the job id is
    unset so the Reports page can render an empty table without crashing.
    """
    if not forward_etl_job_id():
        return []

    w = sp_client()
    runs = w.jobs.list_runs(job_id=_require_job_id(), limit=limit, expand_tasks=False)

    out: list[dict[str, Any]] = []
    for run in runs:
        state = run.state
        life_cycle = state.life_cycle_state if state else None
        result = state.result_state if state else None
        out.append(
            {
                "run_id": run.run_id,
                "life_cycle_state": life_cycle.value if life_cycle else None,
                "result_state": result.value if result else None,
                "start_time": run.start_time,
                "run_page_url": run.run_page_url,
            }
        )
    return out
