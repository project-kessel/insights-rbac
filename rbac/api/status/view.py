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

"""View for server status."""

import logging

from management.group.model import Group
from rest_framework import permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from api.status.model import Status
from api.status.serializer import StatusSerializer
from rbac.env import ENVIRONMENT

logger = logging.getLogger(__name__)

# Cache for _is_seeding_complete. Only cache True (seeding does not get undone).
# Avoids DB hit on every readiness probe once ready.
_SEEDING_COMPLETE_CACHE = None


def _is_seeding_complete():
    """Check if seeding has been completed (by worker or init container).

    We verify seeding by checking for the platform default group (last step in seeding).
    Caches True to avoid DB hit on every readiness probe (every 10s) once ready.
    """
    global _SEEDING_COMPLETE_CACHE

    if _SEEDING_COMPLETE_CACHE is True:
        return True

    try:
        result = Group.objects.public_tenant_only().filter(platform_default=True).exists()
        if result:
            _SEEDING_COMPLETE_CACHE = True
        return result
    except Exception as exc:
        logger.exception("Error checking if seeding is complete: %s", exc)
        return False


@api_view(["GET", "HEAD"])
@permission_classes((permissions.AllowAny,))
def ready(request):
    """Readiness probe: returns 200 when service can accept traffic.

    When MIGRATE_AND_SEED_ON_INIT is True, the service runs its own seeding in the init
    container, so we return 200 immediately without a DB check. When False, the worker
    runs seeding and we check for the platform default group before returning 200.

    Note: This endpoint is intentionally not added to the OpenAPI spec to avoid
    impacting client generation (e.g., generated clients should not include
    infrastructure endpoints like readiness probes).
    """
    migrate_and_seed = ENVIRONMENT.bool("MIGRATE_AND_SEED_ON_INIT", default=True)
    if migrate_and_seed or _is_seeding_complete():
        return Response({"status": "ready"}, status=200)
    return Response({"status": "waiting for seeding"}, status=503)


@api_view(["GET", "HEAD"])
@permission_classes((permissions.AllowAny,))
def status(request):
    """Provide the server status information.

    @api {get} /api/v1/status/ Request server status
    @apiName GetStatus
    @apiGroup Status
    @apiVersion 1.0.0
    @apiDescription Request server status.

    @apiSuccess {Number} api_version The version of the API.
    @apiSuccess {String} commit  The commit hash of the code base.
    @apiSuccess {Object} modules  The code modules found on the server.
    @apiSuccess {Object} platform_info  The server platform information.
    @apiSuccess {String} python_version  The version of python on the server.
    @apiSuccess {String} server_address  The address of the server.
    @apiSuccessExample {json} Success-Response:
        HTTP/1.1 200 OK
        {
            "api_version": 1,
            "commit": "178d2ea",
            "server_address": "127.0.0.1:8000",
            "platform_info": {
                "system": "Darwin",
                "node": "node-1.example.com",
                "release": "17.5.0",
                "version": "Darwin Kernel Version 17.5.0",
                "machine": "x86_64",
                "processor": "i386"
                },
            "python_version": "3.6.1",
            "modules": {
                "coverage": "4.5.1",
                "coverage.version": "4.5.1",
                "coverage.xmlreport": "4.5.1",
                "cryptography": "2.0.3",
                "ctypes": "1.1.0",
                "ctypes.macholib": "1.0",
                "decimal": "1.70",
                "django": "1.11.5",
                "django.utils.six": "1.10.0",
                "django_filters": "1.0.4",
                "http.server": "0.6"
                }
        }
    """
    status_info = Status()
    serializer = StatusSerializer(status_info)
    server_info = serializer.data
    return Response(server_info)
