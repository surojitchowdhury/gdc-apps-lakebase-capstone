#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["databricks-sdk>=0.40"]
# ///
"""T7 — Create/update the forward-ETL Databricks Job (Pattern A).

Uploads the ``forward_etl`` notebook to the workspace under ``PARENT_PATH`` and
creates (or idempotently updates) a **serverless** job named
``capstone-forward-etl`` that runs it. Prints the ``job_id`` — this is the
``FORWARD_ETL_JOB_ID`` that ``app/lib/jobs.py`` triggers and that the bundle
binds to the app at T8.

Idempotent: matched by job name; re-running resets the existing job's settings
rather than creating a duplicate. All operational values come from ``app/.env``
(via ``_common`` / env) — no hard-coded hosts, catalogs, or secrets.

Usage (from repo root):
  uv run lakebase/forward_etl/pattern_a_psycopg2/create_job.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import jobs

# Reuse the reverse-ETL env loader (loads app/.env without overriding).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "reverse_etl"))
import _common as C  # noqa: E402

JOB_NAME = "capstone-forward-etl"
_LOCAL_NOTEBOOK = Path(__file__).resolve().parent / "forward_etl.py"


def _env(name: str, *, required: bool = True, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise SystemExit(f"Required env var {name!r} is not set (check app/.env)")
    return val  # type: ignore[return-value]


def upload_notebook(w: WorkspaceClient, parent_path: str) -> str:
    """Upload the notebook source into the workspace, return its workspace path."""
    from databricks.sdk.service.workspace import ImportFormat, Language

    dest_dir = f"{parent_path}/forward_etl"
    w.workspace.mkdirs(dest_dir)
    notebook_path = f"{dest_dir}/forward_etl"

    source = _LOCAL_NOTEBOOK.read_text()
    import base64

    w.workspace.import_(
        path=notebook_path,
        format=ImportFormat.SOURCE,
        language=Language.PYTHON,
        content=base64.b64encode(source.encode()).decode(),
        overwrite=True,
    )
    print(f"[notebook] uploaded → {notebook_path}")
    return notebook_path


def build_task(notebook_path: str) -> jobs.Task:
    """One serverless notebook task. Passing NO cluster/environment key makes the
    task use serverless compute. Job parameters carry the sandbox defaults; the
    notebook widgets read them (overridable per run via the Jobs API)."""
    return jobs.Task(
        task_key="forward_etl",
        notebook_task=jobs.NotebookTask(
            notebook_path=notebook_path,
            base_parameters={
                "catalog": _env("CAPSTONE_CATALOG"),
                "schema": _env("CAPSTONE_SCHEMA"),
                "pg_instance": _env("PG_INSTANCE_NAME"),
                "pg_database": _env("PGDATABASE"),
                "pg_host": _env("PGHOST"),
            },
        ),
    )


def main() -> None:
    C.load_env()
    parent_path = _env("PARENT_PATH")
    w = WorkspaceClient(profile=_env("DATABRICKS_PROFILE"))

    notebook_path = upload_notebook(w, parent_path)
    task = build_task(notebook_path)

    existing = next((j for j in w.jobs.list(name=JOB_NAME)), None)
    tags = {"capstone": "forward-etl", "pattern": "A-psycopg-merge"}

    if existing:
        w.jobs.reset(
            job_id=existing.job_id,
            new_settings=jobs.JobSettings(
                name=JOB_NAME, tasks=[task], max_concurrent_runs=1, tags=tags
            ),
        )
        job_id = existing.job_id
        print(f"[job] {JOB_NAME} exists — reset settings (job_id={job_id})")
    else:
        created = w.jobs.create(
            name=JOB_NAME, tasks=[task], max_concurrent_runs=1, tags=tags
        )
        job_id = created.job_id
        print(f"[job] created {JOB_NAME} (job_id={job_id})")

    print(f"\nFORWARD_ETL_JOB_ID={job_id}")


if __name__ == "__main__":
    main()
