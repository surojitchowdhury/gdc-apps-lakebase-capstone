"""Customer 360 — Streamlit entry point + navigation.

Multipage app. This file is the landing/overview page and wires up navigation
to the two feature pages via ``st.navigation`` (Streamlit's programmatic
multipage API). The pages themselves live under ``app/pages/`` and call the
data-access layer in :mod:`app.lib.data` directly — there is no HTTP API in a
Streamlit app; the pages *are* the client.

Auth boundaries (see :mod:`app.lib.auth`):

* Lakebase reads/writes run as the app **service principal**.
* The Metrics tab's gold aggregate runs as the **calling user (OBO)**.
"""

from __future__ import annotations

import streamlit as st

from lib.auth import current_user_email

st.set_page_config(
    page_title="Customer 360",
    page_icon="👥",
    layout="wide",
)


def _overview() -> None:
    """Landing page: what the app does + who you're signed in as."""
    st.title("👥 Customer 360")
    st.caption(
        "Databricks Apps + Lakebase capstone — synced customer reads, gold "
        "metrics via the SQL warehouse, and audited notes / segment overrides."
    )

    email = current_user_email()
    if email:
        st.success(f"Signed in as **{email}**")
    else:
        st.info(
            "Running without a forwarded user identity (headless or before OBO "
            "consent). Writes are attributed to a placeholder actor and the "
            "Metrics tab needs the running app with OBO enabled."
        )

    st.subheader("Pages")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(
            "#### 📋 Customers\n"
            "Filter and page through the customer base (server-side pagination). "
            "Select a customer to open their detail view."
        )
    with col2:
        st.markdown(
            "#### 🔎 Customer detail\n"
            "Profile, recent activity, gold metrics (as you, via OBO), and "
            "audited note / segment-override forms."
        )

    st.divider()
    st.markdown(
        "**Auth model** — Lakebase reads & writes run as the app *service "
        "principal*; the Metrics tab's gold query runs as *you* (OBO) so it is "
        "attributed to your identity in the SQL audit log."
    )


# --- Navigation -------------------------------------------------------------
# Programmatic multipage nav. The numeric filename prefixes on the page files
# also make them work with Streamlit's default file-based nav; declaring them
# here lets us control titles/icons and keep the overview as the landing page.
overview_page = st.Page(_overview, title="Overview", icon="🏠", default=True)
customers_page = st.Page("pages/1_Customers.py", title="Customers", icon="📋")
detail_page = st.Page("pages/2_Customer_Detail.py", title="Customer detail", icon="🔎")
dashboard_page = st.Page("pages/3_Dashboard.py", title="Dashboard", icon="📊")
genie_page = st.Page("pages/4_Genie.py", title="Ask Genie", icon="💬")
reports_page = st.Page("pages/5_Reports.py", title="Reports", icon="📈")

nav = st.navigation(
    [overview_page, customers_page, detail_page, dashboard_page, genie_page, reports_page]
)
nav.run()
