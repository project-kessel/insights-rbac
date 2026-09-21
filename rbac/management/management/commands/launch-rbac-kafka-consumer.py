"""Launch RBAC Kafka consumer command."""

import logging
import os
import signal
import sys

import sentry_sdk
from app_common_python import LoadedConfig
from core.constants import CONSUMER_COMPONENT
from core.kafka_consumer import RBACKafkaConsumer
from django.conf import settings
from django.core.management import BaseCommand
from prometheus_client import start_http_server
from sentry_sdk.integrations.logging import LoggingIntegration

logger = logging.getLogger(__name__)


# Benign kafka-python idle/transport messages that are safe to NOT investigate.
# These are logged by kafka-python's internal loggers (logger names starting with "kafka."),
# NOT by RBAC code. They become GlitchTip events because of LoggingIntegration(event_level=logging.ERROR).
# New benign signatures can be added here.
KAFKA_BENIGN_SIGNATURES = [
    "Connection reset by peer",
    "Closing idle connection",
    "Broken pipe",
    "Fetch to node",  # kafka.consumer.fetcher retry noise, e.g. "Fetch to node 3 failed"
    "Errno 104",
    "Errno 32",
]


def _get_event_text_for_filtering(event, hint):
    """Extract all text from event and hint for benign-signature matching.

    Args:
        event: Sentry event dict
        hint: Sentry hint dict (may contain exc_info)

    Returns:
        str: Concatenated text from message and exception fields (lowercased)
    """
    parts = []

    # Extract log message
    logentry = event.get("logentry", {})
    if logentry.get("message"):
        parts.append(logentry["message"])
    if logentry.get("formatted"):
        parts.append(logentry["formatted"])

    # Extract exception text from event
    exception_data = event.get("exception", {})
    if exception_data.get("values"):
        for exc_value in exception_data["values"]:
            if exc_value.get("value"):
                parts.append(exc_value["value"])

    # Extract exception text from hint
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


def _filter_kafka_benign_events(event, hint):
    """Filter out benign kafka-python idle/transport noise events.

    Returns None (drop) ONLY when BOTH conditions are true:
    1. Logger name starts with "kafka."
    2. Message or exception text matches a benign signature

    Otherwise returns the event unchanged.

    This is wrapped in a try/except to ensure we never drop events due to
    unexpected errors in the filter logic (fail-open).
    """
    try:
        logger_name = event.get("logger", "")

        # Only filter events from kafka-python's own loggers
        if not logger_name.startswith("kafka."):
            return event

        # Gather all text from the event
        event_text = _get_event_text_for_filtering(event, hint)

        # Check if any benign signature matches (case-insensitive substring)
        for signature in KAFKA_BENIGN_SIGNATURES:
            if signature.lower() in event_text:
                # This is a known-benign kafka idle/transport event, drop it
                return None

        # Not a benign signature, send it
        return event

    except Exception:
        # On any unexpected error, fail-open: return the event unchanged
        # (never raise from inside before_send)
        return event


def _configure_sentry_integrations():
    """Configure Sentry integrations for the consumer.

    Returns:
        list: List of Sentry integrations to use.
    """
    return [
        LoggingIntegration(
            level=logging.INFO,  # Capture info and above as breadcrumbs
            event_level=logging.ERROR,  # Send errors as events
        )
    ]


def _set_sentry_consumer_context():
    """Set consumer-specific tags and context in Sentry.

    This adds tags and context that will be attached to all Sentry events,
    allowing filtering and grouping of consumer-specific errors in Glitchtip.
    """
    # Set consumer-specific tags that will be attached to all events
    sentry_sdk.set_tag("component", CONSUMER_COMPONENT)
    sentry_sdk.set_tag("service", "rbac")
    sentry_sdk.set_tag("consumer_group", settings.RBAC_KAFKA_CONSUMER_GROUP_ID)

    # Set context with additional consumer information
    sentry_sdk.set_context(
        "consumer",
        {
            "topic": settings.RBAC_KAFKA_CONSUMER_TOPIC,
            "group_id": settings.RBAC_KAFKA_CONSUMER_GROUP_ID,
            "component": CONSUMER_COMPONENT,
        },
    )


def initialize_consumer_sentry():
    """Initialize Sentry/Glitchtip SDK for the consumer with consumer-specific tags.

    This is separate from the main Django settings initialization to allow
    consumer-specific configuration (tags, integrations).

    Note: The return value indicates whether initialization was successful,
    but the consumer will continue to run even if Sentry initialization fails.
    Sentry is optional monitoring - not a hard requirement for consumer operation.
    """
    glitchtip_dsn = os.getenv("GLITCHTIP_DSN", "")
    if not glitchtip_dsn:
        logger.info(f"[{CONSUMER_COMPONENT}] GLITCHTIP_DSN not set, skipping Glitchtip initialization")
        return

    try:
        # Initialize Sentry with consumer-specific configuration
        sentry_sdk.init(
            dsn=glitchtip_dsn,
            integrations=_configure_sentry_integrations(),
            environment=os.getenv("ENV_NAME", "unknown"),
            release=os.getenv("GIT_COMMIT", "unknown"),
            before_send=_filter_kafka_benign_events,
        )

        _set_sentry_consumer_context()

        logger.info(
            f"[{CONSUMER_COMPONENT}] Sentry SDK initialization using Glitchtip was successful! "
            f"(component={CONSUMER_COMPONENT})"
        )

    except Exception:
        logger.exception(f"[{CONSUMER_COMPONENT}] Failed to initialize Sentry/Glitchtip")


class Command(BaseCommand):
    """Command for launching the Kafka consumer for the read-after-writes."""

    help = "Launches the RBAC Kafka consumer with validation and health checks"

    def __init__(self, *args, **kwargs):
        """Initialize the command."""
        super().__init__(*args, **kwargs)
        self.consumer = None

    def add_arguments(self, parser):
        """Add command line arguments."""
        parser.add_argument(
            "--topic",
            type=str,
            help="Kafka topic to consume from (overrides settings)",
        )

    def handle(self, *args, **options):
        """Launch the Kafka consumer."""
        # Initialize Sentry/Glitchtip with consumer-specific configuration
        initialize_consumer_sentry()

        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        try:
            # Start Prometheus metrics HTTP server
            # Use the same port configuration as Celery workers
            # Note: Consumer is single-process, so we use the default REGISTRY
            # (Celery uses a custom registry with MultiProcessCollector for multi-process)
            metrics_port = getattr(LoadedConfig, "metricsPort", 9000)

            try:
                start_http_server(metrics_port, addr="0.0.0.0")
                logger.info(f"Prometheus metrics server started on port {metrics_port}")
            except Exception as e:
                sentry_sdk.capture_exception(e)
                logger.error(f"Failed to start metrics server on port {metrics_port}: {e}")
                # Exit the entire process, we don't want to spin up the consumer without metrics
                sys.exit(1)

            # Create and start consumer
            topic = options.get("topic")
            self.consumer = RBACKafkaConsumer(topic=topic)

            logger.info("Starting RBAC Kafka consumer...")
            self.consumer.start_consuming()

        except KeyboardInterrupt:
            logger.info("Received interrupt signal, shutting down...")
        except Exception as e:
            logger.error(f"Consumer failed: {e}")
            sys.exit(1)
        finally:
            self._cleanup()

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        # Graceful shutdown - SEC-MON-REQ-1 compliance (EOI-5 process_status)
        logger.info(
            f"Received signal {signum}, shutting down gracefully...",
            extra={
                "action": "SHUTDOWN",
                "resource_type": "kafka_consumer",
                "outcome": "in_progress",
                "principal": "system:kafka:consumer",
                "signal": signum,
            },
        )
        cleanup_outcome = "success"
        try:
            self._cleanup()
        except Exception:
            cleanup_outcome = "failure"
            logger.exception("Error during Kafka consumer cleanup")
        # Graceful shutdown - SEC-MON-REQ-1 compliance (EOI-5 process_status)
        logger.info(
            "Kafka consumer shutdown complete",
            extra={
                "action": "SHUTDOWN",
                "resource_type": "kafka_consumer",
                "outcome": cleanup_outcome,
                "principal": "system:kafka:consumer",
            },
        )
        sys.exit(0)

    def _cleanup(self):
        """Clean up resources."""
        if self.consumer:
            self.consumer.stop_consuming()
            self.consumer = None
