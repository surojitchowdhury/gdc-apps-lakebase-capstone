"""Dashboard page — embeds the AI/BI (Lakeview) dashboard in an iframe.

Reps get broader analytics in-app without leaving for the workspace UI. The
supported integration is an iframe embed of the published dashboard:

    {host}/embed/dashboardsv3/{dashboard_id}

Host + dashboard id come from :mod:`app.lib.config` (env, loaded from app/.env
locally / injected by the Apps runtime in production). If ``DASHBOARD_ID`` is
unset we show a clear warning instead of rendering a broken iframe.

IMPORTANT (deploy-time, T8): the embed is blocked by ``X-Frame-Options`` until
the app's domain is ALLOWLISTED in the workspace:

    Settings → Security → External Access → Embed Dashboard →
    add the app host (e.g. customer360-<workspace>.databricksapps.com)

That is a one-time workspace-admin UI step, only relevant once the app is
deployed. It cannot be done from application code and does not block local dev.
"""

from __future__ import annotations

import streamlit as st

from lib import config

_EMBED_HEIGHT = 900

st.title("📊 Analytics dashboard")
st.caption(
    "Embedded AI/BI (Lakeview) dashboard. Broader analytics in-app — no need to "
    "leave for the workspace UI."
)

dashboard_id = config.dashboard_id_optional()
if not dashboard_id:
    st.warning(
        "No dashboard configured. Set **DASHBOARD_ID** in the environment "
        "(app/.env locally, or app.yaml env when deployed) to embed the AI/BI "
        "dashboard here."
    )
    st.stop()

try:
    embed_url = config.dashboard_embed_url()
except RuntimeError as exc:  # pragma: no cover - surfaced live in the app
    st.error(f"Cannot build the dashboard embed URL: `{exc}`")
    st.stop()

# The dashboard renders here only if the app's domain is allowlisted for embed
# in the workspace (see module docstring). Otherwise X-Frame-Options blocks it
# and the frame stays blank — an admin allowlist step is required at deploy time.
st.components.v1.iframe(embed_url, height=_EMBED_HEIGHT)

with st.expander("Dashboard not showing?"):
    st.markdown(
        "If the frame is blank, the app's domain likely isn't allowlisted for "
        "dashboard embedding yet. A workspace admin must add this app's host "
        "under **Settings → Security → External Access → Embed Dashboard**. "
        "Without it, the browser blocks the iframe via `X-Frame-Options`."
    )
