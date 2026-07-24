"""Temporary diagnostic entry point; not part of the feature PR."""

import os

from lib.dashboard_embed import mint_scoped_dashboard_token

token = mint_scoped_dashboard_token()
print(
    f"dashboard_external_embed preflight_scoped_token_present={bool(token)}",
    flush=True,
)

os.execvp(
    "streamlit",
    [
        "streamlit",
        "run",
        "streamlit_app.py",
        "--server.port",
        os.environ["DATABRICKS_APP_PORT"],
        "--server.address",
        "0.0.0.0",
    ],
)
