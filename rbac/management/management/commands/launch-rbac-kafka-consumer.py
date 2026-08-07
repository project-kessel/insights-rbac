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
