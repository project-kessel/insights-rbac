"""Request context variables for automatic log enrichment.

Uses Python's ``contextvars`` module so that request metadata (request_id,
org_id, user_id) is available to every log line within the same request
without requiring explicit parameter passing.

Context variables are thread-safe and async-safe.  Outside a request context
(e.g. Celery tasks, management commands, startup) the default value ``"-"``
is used, which prevents crashes and clearly marks non-request log lines.
"""

import contextvars

#: Unique identifier for the current request.  Populated from the
#: ``X-RH-INSIGHTS-REQUEST-ID`` header or a generated fallback UUID.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

#: Organization identifier extracted from the identity header.
org_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("org_id", default="-")

#: User ID extracted from the identity header.  For service accounts
#: (where ``user_id`` is ``None``) this is set to the ``client_id`` instead,
#: so that every authenticated request has a meaningful identifier.
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("user_id", default="-")

#: Type of the authenticated caller: ``"user"``, ``"service_account"``,
#: or the default ``"-"`` outside a request context.
user_type_var: contextvars.ContextVar[str] = contextvars.ContextVar("user_type", default="-")
