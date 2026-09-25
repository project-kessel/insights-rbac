#
# Copyright 2024 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Shared Sentry/GlitchTip filter for benign kafka-python transport events.

This module provides filtering logic for kafka-python idle/transport noise
that can be reused by both the main Django process (settings.py) and the
Kafka consumer process (launch-rbac-kafka-consumer.py).

IMPORTANT: This module must remain import-safe for settings.py load time.
Do NOT import Django, Kafka, or app_common_python. Only stdlib + prometheus_client.
"""

import logging
import threading
import time
from collections import deque

from prometheus_client import Counter, REGISTRY

logger = logging.getLogger(__name__)


# Benign kafka-python idle/transport messages that are safe to NOT investigate.
# These are transport-specific signatures from kafka-python's internal connection handling.
# They become GlitchTip events because of LoggingIntegration(event_level=logging.ERROR).
# New benign signatures can be added here.
KAFKA_BENIGN_SIGNATURES = [
    "Connection reset by peer",
    "Closing idle connection",
    "Broken pipe",
    "Errno 104",
    "Errno 32",
    # Headline phrasings of the same transient MSK connection churn. These are the
    # top-level messages GlitchTip groups the connection-reset events under, so they
    # are suppressed even when the Errno-104 body text is absent. "Abort" is
    # deliberately NOT listed: as a bare substring it would over-match unrelated events.
    "Metadata refresh: failed",
    "Fetch to node",
    "Connection lost",
]

# Storm breakout thresholds: suppress benign events at low rate, but pass through during a burst.
# MSK idle-recycle produces a few per 5-15 min under normal operation. 10 events/5min clears normal
# noise and breaks out on a reconnect storm (e.g., broker restart, network blip).
STORM_WINDOW_SECONDS = 300
STORM_THRESHOLD = 10

_benign_match_timestamps = deque()
_benign_match_lock = threading.Lock()

try:
    kafka_benign_events_matched_total = Counter(
        "kafka_benign_events_matched_total",
        "Total count of benign kafka-python idle/transport events matched (both suppressed and passed through)",
    )
except ValueError:
    kafka_benign_events_matched_total = REGISTRY._names_to_collectors.get("kafka_benign_events_matched_total")


def _get_monotonic_time():
    """Return current monotonic time. Isolated for testability (can be patched in tests)."""
    return time.monotonic()


def _get_event_text_for_filtering(event, hint):
    """Extract all text from event and hint for benign-signature matching.

    Gathers text from all known Sentry event payload locations, including:
    - Top-level message/title fields
    - metadata.title
    - logentry (older event shape)
    - entries[] with type="message" (production event shape)
    - exception values
    - hint exc_info

    Args:
        event: Sentry event dict
        hint: Sentry hint dict (may contain exc_info)

    Returns:
        str: Concatenated text from all available fields (lowercased)
    """
    parts = []

    # Top-level message field (can be string or dict in different event shapes)
    message = event.get("message")
    if message:
        if isinstance(message, str):
            parts.append(message)
        elif isinstance(message, dict):
            if message.get("message"):
                parts.append(message["message"])
            if message.get("formatted"):
                parts.append(message["formatted"])

    # Top-level title
    if event.get("title"):
        parts.append(event["title"])

    # metadata.title
    metadata = event.get("metadata", {})
    if metadata.get("title"):
        parts.append(metadata["title"])

    # logentry (older event shape, keep for compatibility)
    logentry = event.get("logentry", {})
    if logentry.get("message"):
        parts.append(logentry["message"])
    if logentry.get("formatted"):
        parts.append(logentry["formatted"])

    # entries[] - production event shape has type="message" entries
    entries = event.get("entries", [])
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict) and entry.get("type") == "message":
                data = entry.get("data", {})
                if data.get("message"):
                    parts.append(data["message"])
                if data.get("formatted"):
                    parts.append(data["formatted"])

    # exception values from event
    exception_data = event.get("exception", {})
    if exception_data.get("values"):
        for exc_value in exception_data["values"]:
            if exc_value.get("value"):
                parts.append(exc_value["value"])

    # exception from hint
    exc_info = hint.get("exc_info")
    if exc_info:
        try:
            # exc_info is typically (type, value, traceback) tuple
            if isinstance(exc_info, tuple) and len(exc_info) >= 2:
                exc_instance = exc_info[1]
                if exc_instance:
                    parts.append(str(exc_instance))
        except Exception:
            # If anything goes wrong extracting from hint, continue with what we have
            pass

    return " ".join(parts).lower()


def _is_kafka_origin(event):
    """Check whether the event originated from kafka-python internals.

    An event is considered kafka-origin if EITHER:
    1. The top-level ``logger`` field starts with ``kafka.`` (set by
       LoggingIntegration when a kafka-python logger emits at ERROR), OR
    2. Any breadcrumb has a ``category`` starting with ``kafka.`` (present
       in the production GlitchTip event shape even when ``logger`` is absent).

    This gate prevents non-kafka application errors (e.g. RBAC task errors
    that happen to contain "Connection reset by peer") from being suppressed.

    Args:
        event: Sentry event dict

    Returns:
        bool: True if the event originated from kafka-python internals
    """
    try:
        # Check top-level logger field (set by Sentry LoggingIntegration).
        # When an explicit non-empty logger is present, use it as the
        # authoritative origin signal – fall back to breadcrumbs only when
        # the logger is absent or empty.
        event_logger = event.get("logger", "")
        if isinstance(event_logger, str):
            event_logger = event_logger.strip()
            if event_logger:
                return event_logger.startswith("kafka.")

        # Check breadcrumb categories (production event shape)
        entries = event.get("entries", [])
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("type") == "breadcrumbs":
                    breadcrumb_data = entry.get("data", {})
                    values = breadcrumb_data.get("values", [])
                    if isinstance(values, list):
                        for bc in values:
                            if isinstance(bc, dict):
                                category = bc.get("category", "")
                                if isinstance(category, str) and category.startswith("kafka."):
                                    return True

        # Also check top-level breadcrumbs (alternative Sentry SDK shape)
        breadcrumbs = event.get("breadcrumbs", {})
        if isinstance(breadcrumbs, dict):
            values = breadcrumbs.get("values", [])
            if isinstance(values, list):
                for bc in values:
                    if isinstance(bc, dict):
                        category = bc.get("category", "")
                        if isinstance(category, str) and category.startswith("kafka."):
                            return True

        return False

    except Exception:
        # On any error, fail-open: assume NOT kafka origin (event will be sent)
        return False


def _should_suppress_benign_event():
    """Check if a benign event should be suppressed based on storm breakout logic.

    Returns True if the event should be DROPPED (suppressed), False if it should pass through.

    Storm breakout: suppress benign events at low rate (< STORM_THRESHOLD per STORM_WINDOW_SECONDS),
    but pass through during a burst (>= STORM_THRESHOLD in window). This lets steady low-rate noise
    stay quiet while ensuring a reconnect storm shows up in GlitchTip.

    Boundary: count the matches already in the last 300s (BEFORE counting this one). If that count is
    < STORM_THRESHOLD (i.e. 0-9), suppress (drop) this one; once STORM_THRESHOLD matches are already in
    the window, pass through. So the first 10 benign matches in a 5-min window are dropped and the 11th
    onward break out to GlitchTip.
    """
    try:
        with _benign_match_lock:
            now = _get_monotonic_time()
            cutoff = now - STORM_WINDOW_SECONDS

            # Evict timestamps older than the window
            while _benign_match_timestamps and _benign_match_timestamps[0] < cutoff:
                _benign_match_timestamps.popleft()

            # Check if we're below threshold (suppress) or at/above threshold (pass through)
            count_in_window = len(_benign_match_timestamps)

            # Record this match timestamp for future window calculations
            _benign_match_timestamps.append(now)

            # Suppress (return True = drop) if fewer than STORM_THRESHOLD matches are already in the
            # window; pass through (False) once the window is full. First 10 dropped, 11th+ break out.
            should_suppress = count_in_window < STORM_THRESHOLD
            return should_suppress

    except Exception:
        # On any error in rate-limit logic, fail-open: do NOT suppress (return False = pass through)
        return False


def _filter_kafka_benign_events(event, hint):
    """Filter out benign kafka-python idle/transport noise events.

    An event is dropped (returns None) only when ALL THREE conditions are met:
    1. The event originated from kafka-python internals (logger or breadcrumb origin gate),
    2. The event text matches a benign signature, AND
    3. Storm breakout allows suppression (low-rate steady noise).

    Non-kafka application errors are always sent, even if their text happens to
    contain a benign signature (e.g. an RBAC task wrapping "Connection reset by peer").

    This is wrapped in a try/except to ensure we never drop events due to
    unexpected errors in the filter logic (fail-open).
    """
    try:
        # Origin gate: only consider events from kafka-python internals.
        # Without this, RBAC app errors that happen to contain benign text
        # (e.g. a task wrapping "Connection reset by peer") would be suppressed.
        if not _is_kafka_origin(event):
            return event

        # Gather all text from the event
        event_text = _get_event_text_for_filtering(event, hint)

        # Check if any benign signature matches (case-insensitive substring)
        is_benign = False
        for signature in KAFKA_BENIGN_SIGNATURES:
            if signature.lower() in event_text:
                is_benign = True
                break

        if not is_benign:
            # Not a benign signature, send it
            return event

        # Benign match: increment the counter (tracks volume regardless of suppress/pass-through)
        kafka_benign_events_matched_total.inc()

        # Check storm breakout: should we suppress or pass through?
        if _should_suppress_benign_event():
            # Suppress: drop the event
            return None
        else:
            # Storm breakout: pass through to GlitchTip
            return event

    except Exception:
        # On any unexpected error, fail-open: return the event unchanged
        # (never raise from inside before_send)
        return event
