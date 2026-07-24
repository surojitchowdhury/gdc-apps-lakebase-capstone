"""Customer detail page — Profile / Activity / Notes / Segment tabs.

Selected ``customer_id`` comes from ``st.session_state`` (set on the Customers
page). Data-access boundaries:

* Profile + Activity + write forms → :mod:`app.lib.data` via the app SP.
* Metrics tab → gold aggregates via the **SQL warehouse + OBO** (calling user).
  The OBO client is built with :func:`app.lib.auth.obo_client`; when it is
  ``None`` (OBO preview not enabled / no consent yet) the Metrics tab shows a
  clear message rather than crashing or falling back to the SP.

The metrics query is behind an ``@st.fragment`` + explicit "Load metrics"
button so the expensive OBO warehouse call never blocks first paint of the
profile. Note / Segment writes use ``st.form`` (single submit = one write) and
clear the cached reads afterward so the change shows up immediately.
"""

from __future__ import annotations

import streamlit as st

from lib.auth import current_user_email, obo_client
from lib.data import (
    add_note,
    get_customer,
    get_customer_metrics,
    list_notes,
    override_segment,
)

st.title("🔎 Customer detail")

customer_id = st.session_state.get("selected_customer_id")
if not customer_id:
    st.info("No customer selected. Pick one on the **Customers** page.")
    if st.button("← Go to Customers"):
        st.switch_page("pages/1_Customers.py")
    st.stop()


# --- Cached reads (SP), scoped per customer_id ------------------------------
# Each cached function is keyed by customer_id, so we can invalidate exactly the
# affected customer after a write instead of clearing every user's cache.
@st.cache_data(ttl=30, show_spinner="Loading customer…")
def _load_customer(cid: str):
    return get_customer(cid).to_dict()


@st.cache_data(ttl=30, show_spinner="Loading notes…")
def _load_notes(cid: str):
    return list_notes(cid)


def _invalidate_customer(cid: str) -> None:
    """Invalidate the cached reads for THIS customer only, so a write shows up
    immediately without evicting other users' / customers' cached data.

    ``.clear(cid)`` drops just the entry for ``cid`` from each scoped cache. We
    also bump a shared reads-version counter that the Customers list page mixes
    into its cache key, so the list reflects the change on next view too.
    """
    _load_customer.clear(cid)
    _load_notes.clear(cid)
    st.session_state["reads_version"] = st.session_state.get("reads_version", 0) + 1


try:
    customer = _load_customer(customer_id)
except Exception as exc:  # pragma: no cover - surfaced live in the app
    st.error(
        "Could not load this customer from Lakebase. The synced tables may "
        f"still be provisioning.\n\nDetails: `{exc}`"
    )
    st.stop()

profile = customer["profile"]
if profile is None:
    st.warning(f"Customer `{customer_id}` was not found in `customers_synced`.")
    if st.button("← Back to Customers"):
        st.switch_page("pages/1_Customers.py")
    st.stop()

name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
st.subheader(f"{name or customer_id}  ·  `{customer_id}`")

profile_tab, activity_tab, notes_tab, segment_tab = st.tabs(
    ["Profile", "Activity", "Notes", "Segment"]
)

# --- Profile tab ------------------------------------------------------------
with profile_tab:
    c1, c2, c3 = st.columns(3)
    c1.metric("Lifetime value", f"${profile.get('lifetime_value') or 0:,.2f}")
    c2.metric("Churn score", f"{profile.get('churn_score') or 0:.2f}")
    c3.metric("Segment", profile.get("segment_id") or "—")
    st.json(profile, expanded=False)


# --- Activity tab -----------------------------------------------------------
with activity_tab:
    txns = customer["transactions"]
    st.caption(f"Most recent {len(txns)} transactions (max 20).")
    if txns:
        st.dataframe(
            txns,
            use_container_width=True,
            hide_index=True,
            column_config={
                "amount": st.column_config.NumberColumn("Amount", format="$%.2f"),
            },
        )
    else:
        st.info("No transactions found for this customer (or `transactions_synced` is still provisioning).")


# --- Metrics: fragment so the OBO warehouse query doesn't block first paint --
@st.fragment
def _metrics_fragment(cid: str) -> None:
    st.markdown("#### Gold metrics (via SQL warehouse, run as **you** — OBO)")
    obo = obo_client()
    if obo is None:
        st.warning(
            "**User authorization (OBO) is not enabled yet.** The metrics query "
            "must run as *you* (the calling user) against gold — it never falls "
            "back to the service principal. To enable it: turn on the workspace "
            "**User authorization (preview)** toggle, redeploy, then grant the "
            "one-time consent on first load. Profile and activity above do not "
            "need OBO."
        )
        return

    if st.button("Load metrics", key="load_metrics"):
        try:
            with st.spinner("Querying gold via the SQL warehouse (OBO)…"):
                metrics = get_customer_metrics(cid, obo)
        except Exception as exc:
            st.error(f"Metrics query failed: `{exc}`")
            return

        m1, m2, m3 = st.columns(3)
        m1.metric("Transactions", int(metrics.get("transaction_count") or 0))
        m2.metric("Total spend", f"${metrics.get('total_spend') or 0:,.2f}")
        m3.metric("Avg txn", f"${metrics.get('avg_transaction_amount') or 0:,.2f}")
        m4, m5, m6 = st.columns(3)
        m4.metric("Distinct products", int(metrics.get("distinct_products") or 0))
        m5.metric("Top channel", metrics.get("top_channel") or "—")
        m6.metric("Top category", metrics.get("top_category") or "—")
        st.json(metrics, expanded=False)


# Metrics live on the Profile tab area but only run on demand (button inside the
# fragment) so first paint of the profile is never blocked by the OBO call.
with profile_tab:
    st.divider()
    _metrics_fragment(customer_id)


# --- Notes tab (write: staging + audit, single txn, SP) ---------------------
with notes_tab:
    st.markdown("#### Add a note")
    st.caption(
        "Writes to `customer_notes_staging` **and** `customer_audit_log` in one "
        "transaction. Attributed to your identity."
    )
    with st.form("add_note_form", clear_on_submit=True):
        body = st.text_area("Note", placeholder="e.g. Called about renewal; wants a discount.")
        submitted = st.form_submit_button("Save note")
        if submitted:
            if not body.strip():
                st.warning("Note is empty — nothing saved.")
            else:
                try:
                    res = add_note(customer_id, body.strip(), current_user_email())
                    # Invalidate only THIS customer's cached reads, so the notes
                    # list below re-fetches (and shows the new note) on this run.
                    _invalidate_customer(customer_id)
                    st.success(
                        f"Saved note #{res['note_id']} "
                        f"(audit row #{res['audit_id']}, same transaction)."
                    )
                except Exception as exc:
                    st.error(f"Failed to save note: `{exc}`")

    # Existing notes — read AFTER the form handler so a just-added note (whose
    # write invalidated the scoped cache above) appears immediately.
    st.markdown("#### Existing notes")
    try:
        notes = _load_notes(customer_id)
    except Exception as exc:
        st.error(f"Could not load notes: `{exc}`")
        notes = []
    if notes:
        st.caption(f"{len(notes)} note(s), most recent first.")
        st.dataframe(
            notes,
            use_container_width=True,
            hide_index=True,
            column_config={
                "created_at": st.column_config.DatetimeColumn("Created"),
            },
        )
    else:
        st.info("No notes yet for this customer.")


# --- Segment tab (write: idempotent upsert + audit, single txn, SP) ---------
with segment_tab:
    st.markdown("#### Override segment")
    st.caption(
        "UPSERTs `customer_segment_overrides_staging` (idempotent — one row per "
        "customer) **and** appends `customer_audit_log`, in one transaction."
    )
    with st.form("override_segment_form"):
        new_segment = st.selectbox(
            "New segment",
            options=["SEG_A", "SEG_B", "SEG_C", "SEG_D", "SEG_E"],
        )
        submitted = st.form_submit_button("Apply override")
        if submitted:
            try:
                res = override_segment(customer_id, new_segment, current_user_email())
                _invalidate_customer(customer_id)
                st.success(
                    f"Segment override for `{res['customer_id']}` set to "
                    f"**{res['segment_id']}** (audit row #{res['audit_id']}). "
                    "Re-submitting the same value updates the single override "
                    "row rather than adding a duplicate."
                )
            except Exception as exc:
                st.error(f"Failed to override segment: `{exc}`")


st.divider()
if st.button("← Back to Customers"):
    st.switch_page("pages/1_Customers.py")
