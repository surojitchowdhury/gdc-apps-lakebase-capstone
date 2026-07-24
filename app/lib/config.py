"""Environment / configuration accessors for the Customer 360 app.

This is the single place that loads ``app/.env`` and exposes typed accessors for
the values the app needs (workspace host, dashboard id, …). It is deliberately
**Streamlit-free** so it stays importable and testable from the CLI / headless
contexts and can be reused by the auth and data layers without pulling in the UI.

Loading policy mirrors the Apps runtime contract: values already present in the
environment (injected by the Databricks Apps runtime in production) always win;
``app/.env`` only fills in what is missing when running locally / headlessly.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Locate app/.env (this file lives at app/lib/config.py) -----------------
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_env() -> None:
    """Load ``key=value`` pairs from ``app/.env`` into ``os.environ``.

    Never overrides values already set — the Apps runtime wins in production;
    ``app/.env`` is only a local/headless convenience. Safe to call repeatedly.
    """
    if not _ENV_FILE.exists():
        return
    for raw in _ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


# Populate the environment on import so accessors work regardless of import order.
load_env()


def _require(name: str) -> str:
    """Return a required env var, raising a clear error if unset/empty."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. In production it is provided by the Databricks "
            f"Apps runtime; locally it is read from app/.env."
        )
    return value


def host() -> str:
    """Databricks workspace host URL (e.g. ``https://<workspace>.cloud.databricks.com``).

    Trailing slashes are stripped so callers can safely build URLs by appending
    ``/embed/...`` paths.
    """
    value = _require("DATABRICKS_HOST").rstrip("/")
    if "://" not in value:
        value = f"https://{value}"
    return value


def dashboard_id() -> str:
    """AI/BI (Lakeview) dashboard id to embed. Raises if ``DASHBOARD_ID`` is unset."""
    return _require("DASHBOARD_ID")


def dashboard_id_optional() -> "str | None":
    """Return ``DASHBOARD_ID`` if set, else ``None`` (no raise).

    Useful for UI code that wants to show a friendly warning instead of an error
    when the dashboard has not been configured yet.
    """
    value = os.environ.get("DASHBOARD_ID")
    return value or None


def workspace_id() -> str:
    """Numeric Databricks workspace/org id required by the AI/BI JS client."""
    return _require("DATABRICKS_WORKSPACE_ID")


def genie_space_id() -> str:
    """Genie space id to chat against. Raises if ``GENIE_SPACE_ID`` is unset."""
    return _require("GENIE_SPACE_ID")


def genie_space_id_optional() -> "str | None":
    """Return ``GENIE_SPACE_ID`` if set, else ``None`` (no raise).

    Mirrors :func:`dashboard_id_optional` so the Genie page can show a friendly
    warning instead of an error when the space has not been configured yet.
    """
    value = os.environ.get("GENIE_SPACE_ID")
    return value or None


def genie_space_url(space_id: "str | None" = None) -> str:
    """Return the workspace deep-link URL for a Genie space.

    Pattern: ``{host}/genie/rooms/{space_id}``. Defaults to the configured
    space id when ``space_id`` is omitted.
    """
    return f"{host()}/genie/rooms/{space_id or genie_space_id()}"


def dashboard_published_url() -> str:
    """Return the workspace deep link for the published AI/BI dashboard."""
    return (
        f"{host()}/dashboardsv3/{dashboard_id()}/published"
        f"?o={workspace_id()}"
    )
