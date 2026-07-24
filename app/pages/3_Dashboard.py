"""Dashboard page using Databricks external-user AI/BI embedding.

Basic ``/embed/dashboardsv3`` iframes cannot authenticate from the
``databricksapps.com`` origin because workspace session cookies are not sent
cross-origin. This page instead mints a short-lived, dashboard-scoped token
with the App service principal and passes it to ``@databricks/aibi-client``.
"""

from __future__ import annotations

import json

import streamlit as st

from lib import config
from lib.dashboard_embed import DashboardTokenError, mint_scoped_dashboard_token

_COMPONENT_HEIGHT = 900


@st.cache_data(ttl=3000, show_spinner=False)
def _scoped_dashboard_token() -> str:
    """Mint and cache a dashboard-scoped App-SP token for under its 1h TTL."""
    return mint_scoped_dashboard_token()


def _show_workspace_link(reason: str | None = None) -> None:
    if reason:
        st.warning(f"The in-app dashboard is temporarily unavailable: {reason}.")
    st.link_button(
        "Open Customer 360 dashboard in Databricks",
        config.dashboard_published_url(),
        type="primary",
    )
    st.caption(
        "The dashboard opens in the Databricks workspace. In-app embedding uses "
        "the external-user embedding preview and falls back here if scoped "
        "authorization is unavailable."
    )


st.title("📊 Analytics dashboard")
st.caption("Customer 360 analytics, securely embedded from Databricks AI/BI.")

dashboard_id = config.dashboard_id_optional()
if not dashboard_id:
    st.warning(
        "No dashboard configured. Set **DASHBOARD_ID** in the environment "
        "(app/.env locally, or app.yaml env when deployed) to embed the AI/BI "
        "dashboard here."
    )
    st.stop()

try:
    scoped_token = _scoped_dashboard_token()
    component_config = {
        "instanceUrl": config.host(),
        "workspaceId": config.workspace_id(),
        "dashboardId": dashboard_id,
        "token": scoped_token,
    }
    fallback_url = config.dashboard_published_url()
except (DashboardTokenError, RuntimeError) as exc:
    _show_workspace_link(str(exc))
    st.stop()

# JSON encoding avoids interpolating unescaped configuration into executable JS.
# The token remains inside the sandboxed component document and is never logged.
component_config_json = json.dumps(component_config).replace("</", "<\\/")
component_html = f"""
<div id="dashboard" style="height: {_COMPONENT_HEIGHT}px; width: 100%;"></div>
<div id="embed-error" style="display:none; padding:24px; font-family:sans-serif;">
  <p>The in-app dashboard could not initialize.</p>
  <a href={json.dumps(fallback_url)} target="_blank" rel="noopener noreferrer">
    Open Customer 360 dashboard in Databricks
  </a>
</div>
<script type="module">
  import {{ DatabricksDashboard }} from
    'https://cdn.jsdelivr.net/npm/@databricks/aibi-client@0.0.0-alpha.7/+esm';

  const settings = {component_config_json};
  const container = document.getElementById('dashboard');
  try {{
    const dashboard = new DatabricksDashboard({{ ...settings, container }});
    await dashboard.initialize();
  }} catch (error) {{
    console.error('Databricks AI/BI embed initialization failed', error);
    container.style.display = 'none';
    document.getElementById('embed-error').style.display = 'block';
  }}
</script>
"""
st.components.v1.html(component_html, height=_COMPONENT_HEIGHT, scrolling=True)
