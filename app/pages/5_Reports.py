"""Reports page — trigger the forward-ETL job and watch it run.

Forward-ETL (T7, Pattern A) promotes app-written Lakebase *staging* rows (notes,
segment overrides) into UC **gold** Delta tables. This page is the app surface
for it:

* **Run forward-ETL** button → :func:`lib.jobs.run_forward_etl` (`run_now` as the
  app **service principal**); the returned ``run_id`` is stashed in
  ``st.session_state``.
* A live **status** indicator polls :func:`lib.jobs.get_run` on an auto-refresh
  fragment until the run reaches a terminal state, then links to the run page.
* A **recent runs** table lists the job's last runs.

Forward-ETL is app-level work (not user-scoped), so it runs as the SP — no OBO
needed. If ``FORWARD_ETL_JOB_ID`` is unset (it is bound by the bundle at T8) we
show a clear warning instead of crashing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from lib import jobs

# session_state keys — namespaced so they don't collide with other pages.
_SS_RUN_ID = "forward_etl_run_id"

# How often the status fragment re-polls the run while it is in flight.
_POLL_SECONDS = 5

st.title("📈 Reports — forward-ETL")
st.caption(
    "Promote app-written notes and segment overrides from Lakebase staging into "
    "UC gold Delta tables. Runs on demand as the app service principal."
)

# --- Config: job id ---------------------------------------------------------
job_id = jobs.forward_etl_job_id()
if not job_id:
    st.warning(
        "**FORWARD_ETL_JOB_ID is not set.** The forward-ETL job is bound to the "
        "app by the bundle at deploy time (T8) via `app.yaml` `valueFrom` → the "
        "`forward-etl-job` app resource. Set it in `app/.env` to trigger the job "
        "locally. Until then this page is read-only."
    )
    st.stop()

st.markdown(
    """
    **What this does** — reads `*_staging` rows with `processed = false`,
    `MERGE`s them into the gold Delta targets on their primary key, then marks
    exactly those staging rows `processed = true`. Idempotent: re-running with no
    new rows is a no-op.

    | Staging table | Gold target | MERGE key |
    |---|---|---|
    | `customer_notes_staging` | `customer_notes` | `id` |
    | `customer_segment_overrides_staging` | `customer_segment_overrides` | `customer_id` |
    """
)

# --- Trigger ----------------------------------------------------------------
if st.button("▶️ Run forward-ETL", type="primary"):
    try:
        run_id = jobs.run_forward_etl()
        st.session_state[_SS_RUN_ID] = run_id
        st.success(f"Triggered forward-ETL run **{run_id}**.")
    except Exception as exc:  # pragma: no cover - surfaced live in app
        st.error(f"Failed to trigger the forward-ETL job: `{exc}`")


# --- Live status of the in-flight / last-triggered run ----------------------
@st.fragment(run_every=_POLL_SECONDS)
def _run_status() -> None:
    """Poll the tracked run until terminal, refreshing every few seconds.

    The fragment stops auto-refreshing (``run_every`` is only honoured while the
    fragment reruns) once the run is terminal by simply not scheduling further
    work — we render the terminal state and return.
    """
    run_id = st.session_state.get(_SS_RUN_ID)
    if not run_id:
        return

    try:
        status = jobs.get_run(run_id)
    except Exception as exc:  # pragma: no cover - surfaced live in app
        st.error(f"Could not fetch run {run_id} status: `{exc}`")
        return

    life_cycle = status["life_cycle_state"]
    result = status["result_state"]
    url = status["run_page_url"]

    st.subheader(f"Run {run_id}")
    if not status["is_terminal"]:
        st.info(f"⏳ {life_cycle or 'PENDING'} — polling every {_POLL_SECONDS}s…")
        st.progress(0.5, text=status.get("state_message") or "Running…")
    elif result == "SUCCESS":
        st.success(f"✅ {life_cycle} / {result}")
    else:
        st.error(
            f"❌ {life_cycle} / {result or 'UNKNOWN'}"
            + (f" — {status['state_message']}" if status.get("state_message") else "")
        )

    if url:
        st.link_button("Open run in workspace ↗", url)


_run_status()

st.divider()

# --- Recent runs ------------------------------------------------------------
st.subheader("Recent runs")
try:
    recent = jobs.list_recent_runs(limit=10)
except Exception as exc:  # pragma: no cover - surfaced live in app
    st.error(f"Could not list recent runs: `{exc}`")
    recent = []

if not recent:
    st.caption("No runs yet. Trigger one above.")
else:
    def _fmt_ts(ms: "int | None") -> str:
        if not ms:
            return "—"
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )

    st.dataframe(
        [
            {
                "run_id": r["run_id"],
                "started": _fmt_ts(r["start_time"]),
                "life_cycle": r["life_cycle_state"],
                "result": r["result_state"],
                "run_page": r["run_page_url"],
            }
            for r in recent
        ],
        use_container_width=True,
        hide_index=True,
        column_config={"run_page": st.column_config.LinkColumn("run_page")},
    )
