#
# Copyright 2019 Red Hat, Inc.
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
"""Celery setup."""

from __future__ import absolute_import, unicode_literals

import logging
import os
import sys

import sentry_sdk
from app_common_python import LoadedConfig
from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready
from django.conf import settings
from prometheus_client import CollectorRegistry, multiprocess, start_http_server

logger = logging.getLogger("__name__")

# set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "rbac.settings")

app = Celery("rbac")  # pylint: disable=invalid-name

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

app.conf.beat_schedule = {
    "car-wash-daily": {
        "task": "api.tasks.cross_account_cleanup",
        "schedule": crontab(minute=0, hour=0),
        "args": [],
    },  # noqa: E231, E501
    "schedule-redis-check": {
        "task": "management.tasks.run_redis_cache_health",
        "schedule": 30,
        "args": [],
    },
}

if settings.PRINCIPAL_CLEANUP_DELETION_ENABLED_UMB:
    if settings.UMB_JOB_ENABLED:  # TODO: This is temp flag, remove it after populating user_id
        app.conf.beat_schedule["principal-cleanup-every-minute"] = {
            "task": "management.tasks.principal_cleanup_via_umb",
            "schedule": 60,  # Every 60 second
            "args": [],
        }
else:
    app.conf.beat_schedule["principal-cleanup-every-sevenish-days"] = {
        "task": "management.tasks.principal_cleanup",
        "schedule": crontab(0, 0, day_of_month="7-28/7"),
        "args": [],
    }

if settings.PARITY_CHECK_ENABLED:
    app.conf.beat_schedule["parity-access-checks"] = {
        "task": "management.tasks.run_parity_access_checks_in_worker",
        "schedule": settings.PARITY_CHECK_INTERVAL_SECONDS,
        "args": [],
    }

    # Parse cron expression: "minute hour day_of_month month_of_year day_of_week"
    _parity_cron = settings.PARITY_CHECK_SCHEDULE.split()
    app.conf.beat_schedule["kessel-parity-check"] = {
        "task": "management.tasks.run_kessel_parity_checks_in_worker",
        "schedule": crontab(
            minute=_parity_cron[0] if len(_parity_cron) > 0 else "0",
            hour=_parity_cron[1] if len(_parity_cron) > 1 else "0",
            day_of_month=_parity_cron[2] if len(_parity_cron) > 2 else "*",
            month_of_year=_parity_cron[3] if len(_parity_cron) > 3 else "*",
            day_of_week=_parity_cron[4] if len(_parity_cron) > 4 else "*",
        ),
        "args": [],
    }

# Load task modules from all registered Django app configs.
app.autodiscover_tasks()


@worker_ready.connect
def start_metrics_server(sender=None, **kwargs):
    """Start the metrics server."""
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    metrics_port = LoadedConfig.metricsPort or 9000

    try:
        start_http_server(metrics_port, addr="0.0.0.0", registry=registry)
        logger.info(f"Server started and exposing to port {metrics_port}.")
    except Exception as e:
        sentry_sdk.capture_exception(e)
        logger.error(f"Failed to start metrics server: {e}")
        # Exit the entire process, we don't want to spin up the worker without metrics
        sys.exit(1)
