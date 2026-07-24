"""Server-side OAuth token flow for external-user AI/BI embedding."""

from __future__ import annotations

import base64
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from lib import config

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


def mint_scoped_dashboard_token() -> str:
    """Mint a dashboard-scoped token with ambient App-SP credentials."""
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
