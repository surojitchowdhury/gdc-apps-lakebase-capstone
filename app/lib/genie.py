"""OBO-backed Genie Conversation API helpers for the Customer 360 app.

Genie turns natural-language questions ("which segments saw declining LTV in
Q3?") into SQL server-side and returns an answer plus, when the question maps to
data, a query-result attachment. This module wraps the three Conversation API
calls the chat UI needs and normalises the polling / attachment-fetch dance into
a single plain-dict result shape.

**Identity.** Every function takes an OBO :class:`WorkspaceClient` (the *calling
user's* identity) as its first argument — Genie must run as the real user so
row/column access control and the SQL audit log attribute the query to them. We
never construct a service-principal client here; if OBO is unavailable the page
layer surfaces that rather than silently falling back to the SP.

**No SQL injection surface.** User text is passed to Genie verbatim as the
message ``content``; Genie owns NL→SQL translation and executes the SQL itself.
Nothing user-derived is interpolated into SQL by this app.

Kept Streamlit-free so it stays importable / testable headlessly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.dashboards import GenieMessage, MessageStatus

# Genie message statuses that mean "stop polling" — either the answer is ready
# or it will never arrive. Everything else (SUBMITTED, ASKING_AI,
# EXECUTING_QUERY, …) is transient and we keep polling.
_TERMINAL_STATUSES = frozenset(
    {
        MessageStatus.COMPLETED,
        MessageStatus.FAILED,
        MessageStatus.CANCELLED,
        MessageStatus.QUERY_RESULT_EXPIRED,
    }
)

# Bounded polling: give up after ~30s so the UI never hangs forever waiting on a
# stuck message. Poll roughly once a second.
_POLL_TIMEOUT_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 1.0


class GenieTimeoutError(Exception):
    """Raised when a Genie message never reaches a terminal status in time."""


@dataclass
class StartedConversation:
    """Identifiers returned when a new Genie conversation is started."""

    conversation_id: str
    message_id: str


@dataclass
class GenieResult:
    """Normalised result of a completed (or failed) Genie message.

    ``text`` is the assistant's natural-language answer (may be ``None`` if Genie
    only returned a query). ``columns`` / ``rows`` hold the tabular result of an
    attached query when present (``rows`` is a list of row lists aligned to
    ``columns``). ``status`` is the terminal message status string; ``error``
    carries a message when the status is not COMPLETED.
    """

    status: str
    text: "str | None" = None
    columns: "list[str]" = field(default_factory=list)
    rows: "list[list]" = field(default_factory=list)
    attachment_id: "str | None" = None
    query: "str | None" = None
    error: "str | None" = None

    @property
    def has_table(self) -> bool:
        return bool(self.columns)


def start_conversation(
    obo: WorkspaceClient, space_id: str, content: str
) -> StartedConversation:
    """Start a new Genie conversation with the first user turn.

    Runs as the calling user via ``obo``. Returns the ``conversation_id`` +
    ``message_id`` immediately (does not wait for completion) so the caller can
    poll with :func:`get_message`; keeping the conversation id lets follow-up
    turns reuse the same conversation for context.
    """
    waiter = obo.genie.start_conversation(space_id=space_id, content=content)
    resp = waiter.response
    return StartedConversation(
        conversation_id=resp.conversation_id,
        message_id=resp.message_id,
    )


def create_message(
    obo: WorkspaceClient, space_id: str, conversation_id: str, content: str
) -> str:
    """Add a follow-up message to an existing conversation; return its id.

    Genie uses every prior message in ``conversation_id`` as context, which is
    how multi-turn follow-ups "maintain context". Runs as the calling user.
    """
    waiter = obo.genie.create_message(
        space_id=space_id, conversation_id=conversation_id, content=content
    )
    return waiter.response.message_id


def get_message(
    obo: WorkspaceClient,
    space_id: str,
    conversation_id: str,
    message_id: str,
    *,
    timeout_seconds: float = _POLL_TIMEOUT_SECONDS,
    poll_interval_seconds: float = _POLL_INTERVAL_SECONDS,
) -> GenieResult:
    """Poll a Genie message to terminal status and return a normalised result.

    Polls ``genie.get_message`` until the status is terminal (COMPLETED /
    FAILED / CANCELLED / QUERY_RESULT_EXPIRED) or ``timeout_seconds`` elapses,
    in which case :class:`GenieTimeoutError` is raised. On COMPLETED, extracts
    the assistant text and — when the message carries a query attachment —
    fetches its query result and normalises it into ``columns`` + ``rows``.
    Everything runs as the calling user via ``obo``.
    """
    deadline = time.monotonic() + timeout_seconds
    message: "GenieMessage | None" = None
    while True:
        message = obo.genie.get_message(
            space_id=space_id, conversation_id=conversation_id, message_id=message_id
        )
        if message.status in _TERMINAL_STATUSES:
            break
        if time.monotonic() >= deadline:
            raise GenieTimeoutError(
                f"Genie message did not reach a terminal status within "
                f"{timeout_seconds:.0f}s (last status: "
                f"{_status_name(message.status)})."
            )
        time.sleep(poll_interval_seconds)

    return _normalize(obo, space_id, conversation_id, message_id, message)


def _normalize(
    obo: WorkspaceClient,
    space_id: str,
    conversation_id: str,
    message_id: str,
    message: GenieMessage,
) -> GenieResult:
    """Turn a terminal :class:`GenieMessage` into a :class:`GenieResult`."""
    status_name = _status_name(message.status)

    if message.status != MessageStatus.COMPLETED:
        return GenieResult(
            status=status_name,
            error=message.error.error if message.error else status_name,
        )

    text: "str | None" = None
    attachment_id: "str | None" = None
    query_text: "str | None" = None
    columns: "list[str]" = []
    rows: "list[list]" = []

    for attachment in message.attachments or []:
        # A single message may carry a text attachment, a query attachment, or
        # both. Prefer any human-readable text; fetch table rows for a query.
        if attachment.text and attachment.text.content and not text:
            text = attachment.text.content
        if attachment.query is not None:
            attachment_id = attachment.attachment_id
            query_text = attachment.query.query
            if attachment.query.description and not text:
                text = attachment.query.description

    if attachment_id is not None:
        columns, rows = _fetch_query_result(
            obo, space_id, conversation_id, message_id, attachment_id
        )

    # Fall back to the message content itself if no text attachment was present.
    if text is None and message.content:
        text = message.content

    return GenieResult(
        status=status_name,
        text=text,
        columns=columns,
        rows=rows,
        attachment_id=attachment_id,
        query=query_text,
    )


def _fetch_query_result(
    obo: WorkspaceClient,
    space_id: str,
    conversation_id: str,
    message_id: str,
    attachment_id: str,
) -> "tuple[list[str], list[list]]":
    """Fetch + flatten a query attachment's result into (columns, rows).

    Returns empty lists when the result is missing / expired / empty so the UI
    can degrade to showing just the text answer instead of crashing.
    """
    try:
        result = obo.genie.get_message_attachment_query_result(
            space_id=space_id,
            conversation_id=conversation_id,
            message_id=message_id,
            attachment_id=attachment_id,
        )
    except Exception:
        # Result may have expired or be unavailable — treat as "no table".
        return [], []

    statement = getattr(result, "statement_response", None)
    if statement is None:
        return [], []

    manifest = getattr(statement, "manifest", None)
    schema = getattr(manifest, "schema", None) if manifest else None
    columns = (
        [col.name for col in (schema.columns or [])] if schema and schema.columns else []
    )

    data = getattr(statement, "result", None)
    rows = [list(r) for r in (data.data_array or [])] if data and data.data_array else []

    return columns, rows


def _status_name(status) -> str:
    """Best-effort human name for a MessageStatus enum (or ``None``)."""
    if status is None:
        return "UNKNOWN"
    return getattr(status, "value", None) or str(status)
