# Copyright 2024 Red Hat, Inc.
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
"""Describes the urls and patterns for the management application."""

from django.urls import include, path
from management.role.v2_view import RoleV2ViewSet
from management.views import (
    RoleBindingViewSet,
    WorkspaceViewSet,
)
from rest_framework.routers import DefaultRouter, Route


class V2Router(DefaultRouter):
    """Router for V2 view sets."""

    routes = DefaultRouter.routes + [
        Route(
            url=r"^{prefix}:batchCreate{trailing_slash}$",
            mapping={"post": "batch_create"},
            name="{basename}-batch-create",
            detail=False,
            initkwargs={"suffix": "BatchCreate"},
        ),
        Route(
            url=r"^{prefix}:batchDelete{trailing_slash}$",
            mapping={"post": "bulk_destroy"},
            name="{basename}-bulk-destroy",
            detail=False,
            initkwargs={"suffix": "BulkDestroy"},
        ),
    ]


ROUTER = V2Router()
ROUTER.register(r"workspaces", WorkspaceViewSet, basename="workspace")
ROUTER.register(r"role-bindings", RoleBindingViewSet, basename="role-bindings")
ROUTER.register(r"roles", RoleV2ViewSet, basename="roles")

# pylint: disable=invalid-name
urlpatterns = [
    path("", include(ROUTER.urls)),
]
