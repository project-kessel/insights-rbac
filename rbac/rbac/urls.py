#
#    Copyright 2019 Red Hat, Inc.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""rbac URL Configuration.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/2.0/topics/http/urls/
"""

import os

from django.conf import settings
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path, re_path

API_PATH_PREFIX = os.getenv("API_PATH_PREFIX", "api/")
if API_PATH_PREFIX != "":
    if API_PATH_PREFIX.startswith("/"):
        API_PATH_PREFIX = API_PATH_PREFIX[1:]
    if not API_PATH_PREFIX.endswith("/"):
        API_PATH_PREFIX = API_PATH_PREFIX + "/"


# pylint: disable=invalid-name
urlpatterns = [
    re_path(r"^{}v1/".format(API_PATH_PREFIX), include(("api.urls", "v1_api"))),
    re_path(r"^{}v1/".format(API_PATH_PREFIX), include(("management.urls", "v1_management"))),
    path("_private/", include(("internal.urls", "internal"))),
    path("", include("django_prometheus.urls")),
]

if settings.MCP_ENABLED:
    urlpatterns.insert(2, path(settings.A2S_PATH_PREFIX.lstrip("/"), include(("management.mcp_urls", "mcp"))))


if settings.V2_APIS_ENABLED:
    urlpatterns.extend(
        [
            re_path(
                r"^{}v2/".format(API_PATH_PREFIX),
                include(("api.v2_urls", "v2_api")),
            ),
            re_path(
                r"^{}v2/".format(API_PATH_PREFIX),
                include(("management.v2_urls", "v2_management")),
            ),
        ]
    )

urlpatterns += staticfiles_urlpatterns()
