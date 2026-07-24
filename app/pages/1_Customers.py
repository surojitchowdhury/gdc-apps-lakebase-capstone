"""Customers list page — filter widgets + server-side paginated table.

Reads through :func:`app.lib.data.list_customers` (Lakebase ``customers_synced``
via the app SP). Pagination is **server-side**: only one page (``page_size``
rows, capped at 100) is ever fetched — we never load the full ~10k-row table.
The current page number lives in ``st.session_state['page']`` and is reset
whenever a filter changes.

Selecting a row records the ``customer_id`` in ``st.session_state`` and
navigates to the detail page via ``st.switch_page``.
"""

from __future__ import annotations

import streamlit as st

from lib.data import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, list_customers

st.title("📋 Customers")

# --- Filter widgets ---------------------------------------------------------
# Keep filter values in session_state so we can detect a change and reset the
# page back to 1 (paging into a filtered-away region would show an empty page).
with st.sidebar:
    st.header("Filters")
    segment = st.selectbox(
        "Segment",
        options=["(any)", "SEG_A", "SEG_B", "SEG_C", "SEG_D", "SEG_E"],
        index=0,
        help="Exact segment_id match.",
    )
    min_ltv = st.number_input(
        "Min lifetime value",
        min_value=0.0,
        value=0.0,
        step=100.0,
        help="Only customers with lifetime_value at or above this.",
    )
    max_churn = st.slider(
        "Max churn score",
        min_value=0.0,
        max_value=1.0,
        value=1.0,
        step=0.05,
        help="Only customers with churn_score at or below this.",
    )
    page_size = st.slider(
        "Page size",
        min_value=5,
        max_value=MAX_PAGE_SIZE,
        value=DEFAULT_PAGE_SIZE,
        step=5,
    )

# Normalize filter inputs into the values the data layer expects.
segment_arg = None if segment == "(any)" else segment
min_ltv_arg = None if min_ltv <= 0 else float(min_ltv)
max_churn_arg = None if max_churn >= 1.0 else float(max_churn)

# Reset to page 1 whenever any filter/page-size changes.
filter_key = (segment_arg, min_ltv_arg, max_churn_arg, page_size)
if st.session_state.get("_customers_filter_key") != filter_key:
    st.session_state["_customers_filter_key"] = filter_key
    st.session_state["page"] = 1

st.session_state.setdefault("page", 1)


# --- Cached fetch -----------------------------------------------------------
# Cache the current page so widget interactions that don't change inputs (e.g.
# selecting a row) don't re-query Lakebase. Returns a plain dict so it is cache-
# friendly. Writes on the detail page clear this cache so changes appear.
@st.cache_data(ttl=30, show_spinner="Loading customers…")
def _load_page(segment, min_ltv, max_churn, page, page_size):
    return list_customers(
        segment=segment,
        min_ltv=min_ltv,
        max_churn=max_churn,
        page=page,
        page_size=page_size,
    ).to_dict()


page = st.session_state["page"]
try:
    result = _load_page(segment_arg, min_ltv_arg, max_churn_arg, page, page_size)
except Exception as exc:  # pragma: no cover - surfaced live in the app
    st.error(
        "Could not load customers from Lakebase. The synced tables "
        "(`customers_synced`) may still be provisioning.\n\n"
        f"Details: `{exc}`"
    )
    st.stop()

items = result["items"]
total = result["total"]
total_pages = max(1, (total + page_size - 1) // page_size)

# Clamp the page if filters shrank the result set below the current page.
if page > total_pages:
    st.session_state["page"] = total_pages
    st.rerun()

# --- Pagination controls ----------------------------------------------------
st.caption(
    f"**{total}** customers match • page **{page}** of **{total_pages}** "
    f"• {page_size} per page (server-side)"
)
prev_col, next_col, _ = st.columns([1, 1, 6])
with prev_col:
    if st.button("← Prev", disabled=page <= 1, use_container_width=True):
        st.session_state["page"] = page - 1
        st.rerun()
with next_col:
    if st.button("Next →", disabled=page >= total_pages, use_container_width=True):
        st.session_state["page"] = page + 1
        st.rerun()

# --- Table + row selection --------------------------------------------------
if not items:
    st.info("No customers match the current filters.")
    st.stop()

event = st.dataframe(
    items,
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "lifetime_value": st.column_config.NumberColumn("Lifetime value", format="$%.2f"),
        "churn_score": st.column_config.NumberColumn("Churn", format="%.2f"),
    },
)

selected_rows = event.selection.rows if event and event.selection else []
if selected_rows:
    selected = items[selected_rows[0]]
    st.session_state["selected_customer_id"] = selected["customer_id"]
    st.switch_page("pages/2_Customer_Detail.py")
