"""Temporary static client diagnostic; not part of the feature PR."""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

from lib import config
from lib.dashboard_embed import mint_scoped_dashboard_token

token = mint_scoped_dashboard_token()
print(
    f"dashboard_external_embed preflight_scoped_token_present={bool(token)}",
    flush=True,
)

settings = json.dumps(
    {
        "instanceUrl": config.host(),
        "workspaceId": config.workspace_id(),
        "dashboardId": config.dashboard_id(),
        "token": token,
    }
)
page = f"""<!doctype html><html><body>
<div id="dashboard" style="height:900px;width:100%"></div>
<script type="module">
import {{DatabricksDashboard}} from 'https://cdn.jsdelivr.net/npm/@databricks/aibi-client@0.0.0-alpha.7/+esm';
const settings = {settings};
const dashboard = new DatabricksDashboard({{...settings, container: document.getElementById('dashboard')}});
await dashboard.initialize();
console.log('AIBI_INITIALIZE_SUCCEEDED');
</script></body></html>""".encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


HTTPServer(("0.0.0.0", int(os.environ["DATABRICKS_APP_PORT"])), Handler).serve_forever()
