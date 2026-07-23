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

"""View for openapi documentation."""

import json
import logging
import os

from rest_framework import permissions, status
from rest_framework.decorators import api_view, permission_classes, renderer_classes
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from rbac.settings import BASE_DIR

logger = logging.getLogger(__name__)

OPENAPI_FILE_PATH = os.path.join(BASE_DIR, "..", "docs/source/specs")
OPENAPI_FILE_NAME = "openapi.json"

OPENAPI_V2_FILE_PATH = os.path.join(BASE_DIR, "..", "docs/source/specs/v2")
OPENAPI_V2_FILE_NAME = "openapi.json"


@api_view(["GET"])
@permission_classes((permissions.AllowAny,))
@renderer_classes((JSONRenderer,))
def openapi(request):
    """Provide the openapi information."""
    openapidoc = os.path.join(OPENAPI_FILE_PATH, OPENAPI_FILE_NAME)

    try:
        with open(openapidoc, encoding="utf-8") as api_file:
            data = json.load(api_file)
            return Response(data)
    except FileNotFoundError:
        logger.error(f"OpenAPI specification file not found at {openapidoc}")
        return Response(
            {
                "errors": [
                    {
                        "detail": "OpenAPI specification file not found.",
                        "status": "500",
                    }
                ]
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in OpenAPI specification file: {e}")
        return Response(
            {
                "errors": [
                    {
                        "detail": "OpenAPI specification file contains invalid JSON.",
                        "status": "500",
                    }
                ]
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes((permissions.AllowAny,))
@renderer_classes((JSONRenderer,))
def openapi_v2(request):
    """Provide the V2 openapi information."""
    openapidoc = os.path.join(OPENAPI_V2_FILE_PATH, OPENAPI_V2_FILE_NAME)

    try:
        with open(openapidoc, encoding="utf-8") as api_file:
            data = json.load(api_file)
            return Response(data)
    except FileNotFoundError:
        logger.error(f"V2 OpenAPI specification file not found at {openapidoc}")
        return Response(
            {
                "errors": [
                    {
                        "detail": "V2 OpenAPI specification file not found.",
                        "status": "500",
                    }
                ]
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in V2 OpenAPI specification file: {e}")
        return Response(
            {
                "errors": [
                    {
                        "detail": "V2 OpenAPI specification file contains invalid JSON.",
                        "status": "500",
                    }
                ]
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
