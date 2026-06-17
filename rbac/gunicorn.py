"""Gunicorn configuration file."""

import multiprocessing
import os

from prometheus_client import multiprocess

from rbac.env import ENVIRONMENT

CLOWDER_PORT = "8080"
if ENVIRONMENT.bool("CLOWDER_ENABLED", default=False):
    from app_common_python import LoadedConfig

    CLOWDER_PORT = LoadedConfig.publicPort

bind = f"0.0.0.0:{CLOWDER_PORT}"

cpu_resources = int(os.environ.get("POD_CPU_LIMIT", multiprocessing.cpu_count()))
workers = cpu_resources * int(os.environ.get("GUNICORN_WORKER_MULTIPLIER", 2))
threads = int(os.environ.get("GUNICORN_THREAD_LIMIT", 10))
limit_request_field_size = 16380
# HBI team is requesting with 100 workspace ids in the url.
# So we need to increase the limit_request_line from default 4094 to 4200.
limit_request_line = 4200


def child_exit(server, worker):
    """Watches for workers to exit and marks them as dead in prometheus."""
    # See: https://prometheus.github.io/client_python/multiprocess/
    multiprocess.mark_process_dead(worker.pid)
