"""Genie chat page — ask the customer data questions in plain English.

Reps type natural-language questions ("top segment by LTV") and Genie answers
with a written summary plus, where the question maps to data, a result table.
Built on native Streamlit chat primitives (``st.chat_input`` +
``st.chat_message``).

**Multi-turn context.** The Genie ``conversation_id`` and the full message list
are persisted in ``st.session_state`` so they survive Streamlit reruns. The
first user turn calls :func:`lib.genie.start_conversation`; every subsequent
turn calls :func:`lib.genie.create_message` on the *same* conversation, which is
what lets follow-up questions ("...and which of those declined in Q3?") keep
context.

**Identity.** The chat runs as the calling user via OBO
(``auth.obo_client()``). Genie must run as the real user for access control +
audit, so if OBO is unavailable we explain that and stop — we never fall back to
the service principal for Genie.
"""

from __future__ import annotations

import streamlit as st

from lib import auth, config, genie

# session_state keys — namespaced so they don't collide with other pages.
_SS_CONVERSATION = "genie_conversation_id"
_SS_MESSAGES = "genie_messages"  # list[dict]: {role, text, columns, rows, query, error}

st.title("💬 Ask Genie")
st.caption(
    "Ask ad-hoc questions about the customer data in plain English. Follow-ups "
    "stay in the same conversation, so Genie keeps context across turns."
)

# --- Config: Genie space id -------------------------------------------------
space_id = config.genie_space_id_optional()
if not space_id:
    st.warning(
        "No Genie space configured. Set **GENIE_SPACE_ID** in the environment "
        "(app/.env locally, or app.yaml env when deployed) to enable chat."
    )
    st.stop()

# Deep-link to the space in the workspace UI (top-right).
st.link_button("Open in workspace ↗", config.genie_space_url(space_id))

# --- Identity: OBO only (no SP fallback for Genie) --------------------------
obo = auth.obo_client()
if obo is None:
    st.info(
        "Genie chat runs **as you** (on-behalf-of-user), so it needs user "
        "authorization enabled and consented. Enable **User authorization "
        "(preview)** on the workspace and grant the one-time consent for the "
        "`dashboards.genie` scope on first load. Until then chat is disabled — "
        "the app will not fall back to its service principal for Genie."
    )
    st.stop()

# --- Persisted conversation state -------------------------------------------
if _SS_MESSAGES not in st.session_state:
    st.session_state[_SS_MESSAGES] = []
if _SS_CONVERSATION not in st.session_state:
    st.session_state[_SS_CONVERSATION] = None


def _render_message(msg: dict) -> None:
    """Render one persisted chat message (text answer + optional table)."""
    with st.chat_message(msg["role"]):
        if msg.get("error"):
            st.error(msg["error"])
        if msg.get("text"):
            st.markdown(msg["text"])
        if msg.get("columns"):
            st.dataframe(
                {col: [row[i] for row in msg["rows"]] for i, col in enumerate(msg["columns"])},
                use_container_width=True,
            )
        if msg.get("query"):
            with st.expander("Generated SQL"):
                st.code(msg["query"], language="sql")


# Replay the conversation so far (session_state survives reruns).
for msg in st.session_state[_SS_MESSAGES]:
    _render_message(msg)

# --- New user turn ----------------------------------------------------------
prompt = st.chat_input("Ask about customers, segments, LTV…")
if prompt:
    # Echo + persist the user turn immediately.
    user_msg = {"role": "user", "text": prompt}
    st.session_state[_SS_MESSAGES].append(user_msg)
    _render_message(user_msg)

    with st.chat_message("assistant"):
        with st.status("Asking Genie…", expanded=False) as status:
            try:
                conversation_id = st.session_state[_SS_CONVERSATION]
                if conversation_id is None:
                    # First turn: start a brand-new conversation.
                    started = genie.start_conversation(obo, space_id, prompt)
                    st.session_state[_SS_CONVERSATION] = started.conversation_id
                    conversation_id = started.conversation_id
                    message_id = started.message_id
                else:
                    # Follow-up: reuse the conversation so Genie keeps context.
                    message_id = genie.create_message(
                        obo, space_id, conversation_id, prompt
                    )

                status.update(label="Waiting for the answer…")
                result = genie.get_message(
                    obo, space_id, conversation_id, message_id
                )
                status.update(label="Done", state="complete")
            except genie.GenieTimeoutError as exc:
                status.update(label="Timed out", state="error")
                assistant_msg = {
                    "role": "assistant",
                    "error": (
                        f"Genie took too long to answer: {exc} Please try again "
                        f"or simplify the question."
                    ),
                }
                st.session_state[_SS_MESSAGES].append(assistant_msg)
                st.rerun()
            except Exception as exc:  # pragma: no cover - surfaced live in app
                status.update(label="Error", state="error")
                assistant_msg = {
                    "role": "assistant",
                    "error": f"Genie request failed: `{exc}`",
                }
                st.session_state[_SS_MESSAGES].append(assistant_msg)
                st.rerun()

        # Normalise the terminal result into a persisted assistant message.
        assistant_msg: dict = {"role": "assistant"}
        if result.status != "COMPLETED":
            assistant_msg["error"] = (
                f"Genie could not answer (status: {result.status})."
                + (f" {result.error}" if result.error else "")
            )
        assistant_msg["text"] = result.text
        assistant_msg["columns"] = result.columns
        assistant_msg["rows"] = result.rows
        assistant_msg["query"] = result.query
        st.session_state[_SS_MESSAGES].append(assistant_msg)
    # Re-run so the freshly appended assistant message renders through the same
    # path as the replayed history (single source of truth for rendering).
    st.rerun()
