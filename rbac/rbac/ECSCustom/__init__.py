#
# Copyright 2021 Red Hat, Inc.
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
"""Custom ecs formatter."""

from urllib.parse import urlparse

import django
from ecs_logging import StdlibFormatter


class ECSCustomFormatter(StdlibFormatter):
    """Custom ECS formatter."""

    def format_to_ecs(self, record):
        """Over write the format_to_ecs method to deal with WSGI and socket request."""
        request = None
        if hasattr(record, "request"):
            request = record.request
            record.request = None

        result = super().format_to_ecs(record)

        if request is not None:
            # Deal with socket request, example:
            # "<socket.socket fd=9,
            # family=AddressFamily.AF_INET,
            # type=SocketKind.SOCK_STREAM,
            # proto=0,
            # laddr=('127.0.0.1', 8080), raddr=('127.0.0.1', 52219)>"
            # Currently no valuable info to extract

            if isinstance(request, django.core.handlers.wsgi.WSGIRequest):
                result = self.add_info_from_WSGIRequest(result, request)

        if hasattr(record, "env_name"):
            result["labels"] = result.get("labels", {})
            result["labels"]["env"] = record.env_name

        # Remove some field not following standard:
        # https://www.elastic.co/guide/en/ecs/1.6/ecs-field-reference.html
        result.pop("message", None)
        result.pop("status_code", None)
        result.pop("server_time", None)
        return result

    def add_info_from_WSGIRequest(self, result, request):
        """Extract info from WSGIRequest and add it to the log."""
        parsed_url = urlparse(request.build_absolute_uri())
        request_body_bytes = request.headers.get("Content-Length", "0")

        result["url"] = {"path": parsed_url.path, "domain": parsed_url.hostname, "port": parsed_url.port}
        result["http"] = {
            "request": {"body": {"bytes": int(request_body_bytes)}, "method": request.method},
            "response": {
                "body": {"bytes": len(result["message"]), "content": result["message"]},
                "status_code": result.get("status_code", ""),
            },
        }

        return result
