"""Dashboard page using Databricks external-user AI/BI embedding.

Basic ``/embed/dashboardsv3`` iframes cannot authenticate from the
``databricksapps.com`` origin because workspace session cookies are not sent
cross-origin. This page instead mints a short-lived, dashboard-scoped token
with the App service principal and passes it to ``@databricks/aibi-client``.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import streamlit as st

from lib import config

_COMPONENT_HEIGHT = 900
_EXTERNAL_VIEWER_ID = "customer360-app"
_EXTERNAL_VALUE = "all"


class DashboardTokenError(RuntimeError):
    """A safe-to-display failure from the external-embed token flow."""


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
    step: str,
) -> dict[str, Any]:
    body = urlencode(form).encode() if form is not None else None
    request = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed workspace host
            status = response.status
            payload = json.load(response)
    except HTTPError as exc:
        print(f"dashboard_external_embed {step}_http_status={exc.code}", flush=True)
        raise DashboardTokenError(f"{step} returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"dashboard_external_embed {step}_transport_error={type(exc).__name__}", flush=True)
        raise DashboardTokenError(f"{step} could not reach the workspace") from exc

    print(f"dashboard_external_embed {step}_http_status={status}", flush=True)
    if not isinstance(payload, dict):
        raise DashboardTokenError(f"{step} returned an invalid response")
    return payload


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return f"Basic {credentials}"


@st.cache_data(ttl=3000, show_spinner=False)
def _scoped_dashboard_token() -> str:
    """Mint and cache a dashboard-scoped App-SP token for under its 1h TTL."""
    host = config.host()
    dashboard_id = config.dashboard_id()
    client_id = os.environ.get("DATABRICKS_CLIENT_ID")
    client_secret = os.environ.get("DATABRICKS_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise DashboardTokenError(
            "App service-principal credentials are unavailable in this runtime"
        )

    basic_header = _basic_auth_header(client_id, client_secret)
    token_url = f"{host}/oidc/v1/token"
    sp_token_payload = _request_json(
        "POST",
        token_url,
        headers={
            "Authorization": basic_header,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        form={"grant_type": "client_credentials", "scope": "all-apis"},
        step="step1_sp_token",
    )
    sp_token = sp_token_payload.get("access_token")
    if not isinstance(sp_token, str) or not sp_token:
        raise DashboardTokenError("step1_sp_token returned no access token")

    tokeninfo_url = (
        f"{host}/api/2.0/lakeview/dashboards/{dashboard_id}/published/tokeninfo?"
        + urlencode(
            {
                "external_viewer_id": _EXTERNAL_VIEWER_ID,
                "external_value": _EXTERNAL_VALUE,
            }
        )
    )
    tokeninfo = _request_json(
        "GET",
        tokeninfo_url,
        headers={"Authorization": f"Bearer {sp_token}"},
        step="step2_tokeninfo",
    )
    authorization_details = tokeninfo.get("authorization_details")
    scope = tokeninfo.get("scope")
    if authorization_details is None or not isinstance(scope, str) or not scope:
        raise DashboardTokenError("step2_tokeninfo returned incomplete token metadata")

    scoped_form = {
        "grant_type": "client_credentials",
        "scope": scope,
        "authorization_details": json.dumps(authorization_details),
    }
    custom_claim = tokeninfo.get("custom_claim")
    if custom_claim is not None:
        scoped_form["custom_claim"] = (
            custom_claim if isinstance(custom_claim, str) else json.dumps(custom_claim)
        )

    scoped_payload = _request_json(
        "POST",
        token_url,
        headers={
            "Authorization": basic_header,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        form=scoped_form,
        step="step3_scoped_token",
    )
    scoped_token = scoped_payload.get("access_token")
    if not isinstance(scoped_token, str) or not scoped_token:
        raise DashboardTokenError("step3_scoped_token returned no access token")
    return scoped_token


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
