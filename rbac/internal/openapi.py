#
# Copyright 2022 Red Hat, Inc.
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

from rest_framework import permissions
from rest_framework.decorators import api_view, permission_classes, renderer_classes
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

OPENAPI_FILE_PATH_DEFAULT = "internal/specs"
OPENAPI_FILE_NAME = "openapi.json"


@api_view(["GET"])
@permission_classes((permissions.AllowAny,))
@renderer_classes((JSONRenderer,))
def openapi(request):
    """Provide the openapi information."""
    openapidoc = "{}/{}".format(OPENAPI_FILE_PATH_DEFAULT, OPENAPI_FILE_NAME)
    with open(openapidoc) as api_file:
        data = json.load(api_file)
        return Response(data)
