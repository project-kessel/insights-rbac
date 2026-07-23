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

"""Common pagination class."""

import logging
import re
from urllib.parse import urlparse

from rest_framework.exceptions import ValidationError
from rest_framework.pagination import CursorPagination, LimitOffsetPagination
from rest_framework.response import Response
from rest_framework.utils.urls import replace_query_param

PATH_INFO = "PATH_INFO"
logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class StandardResultsSetPagination(LimitOffsetPagination):
    """Create standard pagination class with page size."""

    default_limit = 10
    max_limit = 1000

    @staticmethod
    def link_rewrite(request, link):
        """Rewrite the link based on the path header to only provide partial url."""
        url = link
        if PATH_INFO in request.META:
            url_components = urlparse(link)
            path_and_query = url_components.path + (f"?{url_components.query}" if url_components.query else "")
            if bool(re.search("/v[0-9]/", path_and_query)):
                url = path_and_query
            else:
                logger.warning(f"Unable to rewrite link as no version was not found in {path_and_query}.")
        return url

    def get_first_link(self):
        """Create first link with partial url rewrite."""
        url = self.request.build_absolute_uri()
        offset = 0
        first_link = replace_query_param(url, self.offset_query_param, offset)
        first_link = replace_query_param(first_link, self.limit_query_param, self.limit)
        return StandardResultsSetPagination.link_rewrite(self.request, first_link)

    def get_next_link(self):
        """Create next link with partial url rewrite."""
        next_link = super().get_next_link()
        if next_link is None:
            return next_link
        return StandardResultsSetPagination.link_rewrite(self.request, next_link)

    def get_previous_link(self):
        """Create previous link with partial url rewrite."""
        previous_link = super().get_previous_link()
        if previous_link is None:
            return previous_link
        return StandardResultsSetPagination.link_rewrite(self.request, previous_link)

    def get_last_link(self):
        """Create last link with partial url rewrite."""
        url = self.request.build_absolute_uri()
        offset = self.count - self.limit if (self.count - self.limit) >= 0 else 0
        last_link = replace_query_param(url, self.offset_query_param, offset)
        last_link = replace_query_param(last_link, self.limit_query_param, self.limit)
        return StandardResultsSetPagination.link_rewrite(self.request, last_link)

    def get_paginated_response(self, data):
        """Override pagination output."""
        return Response(
            {
                "meta": {"count": self.count, "limit": self.limit, "offset": self.offset},
                "links": {
                    "first": self.get_first_link(),
                    "next": self.get_next_link(),
                    "previous": self.get_previous_link(),
                    "last": self.get_last_link(),
                },
                "data": data,
            }
        )


class WSGIRequestResultsSetPagination(StandardResultsSetPagination):
    """Create pagination class with page size and internal flag."""

    def get_limit(self, request):
        """Get limit from query params."""
        request.query_params = request.GET
        return super().get_limit(request)


class V2ResultsSetPagination(StandardResultsSetPagination):
    """V2 pagination class."""

    NO_LIMIT_ENFORCED_VALUE = "-1"

    def paginate_queryset(self, queryset, request, view=None):
        """Override paginate_queryset for V2."""
        request_limit = request.query_params.get(self.limit_query_param)
        if request_limit == self.NO_LIMIT_ENFORCED_VALUE:
            self.max_limit = None
            self.default_limit = queryset.count()

        return super().paginate_queryset(queryset, request, view)


class V2CursorPagination(CursorPagination):
    """Cursor-based pagination for V2 APIs.

    Uses cursor-based pagination which provides consistent ordering
    and better performance for large datasets.

    Supports dynamic ordering via the order_by query parameter.
    Ordering REQUIRES dot notation (e.g., group.name, role.name, user.username).
    Direct field names without dot notation are not allowed.
    Multiple fields can be specified via comma-separated values
    or multiple order_by parameters.

    Available ordering fields (for subject_type=group):
    - group.name, group.description, group.user_count, group.uuid,
      group.created, group.modified
    - role.name, role.uuid, role.created, role.modified
    - last_modified (max RoleBindingGroup/RoleBindingPrincipal created for the resource)

    Available ordering fields (for subject_type=user):
    - user.username, user.uuid
    - role.name, role.uuid, role.created, role.modified
    - last_modified (max RoleBindingGroup/RoleBindingPrincipal created for the resource)

    Available ordering fields (for list endpoint, RoleBinding model):
    - role.name, role.uuid, role.id, role.created, role.modified
    - resource.id, resource.type
    """

    page_size = 10
    page_size_query_param = "limit"
    max_page_size = 1000
    ordering = "-modified"
    cursor_query_param = "cursor"
    NO_LIMIT_ENFORCED_VALUE = "-1"

    def paginate_queryset(self, queryset, request, view=None):
        """Override paginate_queryset to support limit=-1 for no pagination.

        Only overrides default behavior when limit=-1 is explicitly requested.
        All other cases (no limit, limit=0, limit=10, etc.) use standard pagination.
        """
        request_limit = request.query_params.get(self.page_size_query_param)

        # Only disable pagination when limit=-1 is explicitly requested
        # If no limit is provided (None), falls through to default pagination
        if request_limit == self.NO_LIMIT_ENFORCED_VALUE:
            self.request = request
            # Apply ordering before converting to list
            ordering = self.get_ordering(request, queryset, view)
            if ordering:
                queryset = queryset.order_by(*ordering)
            self.page = list(queryset)
            return self.page

        # Use default cursor pagination for all other cases
        return super().paginate_queryset(queryset, request, view)

    # Mapping of dot notation fields to Django ORM fields for Group queryset
    GROUP_FIELD_MAPPING = {
        # Group fields
        "group.name": "name",
        "group.description": "description",
        "group.user_count": "principalCount",
        "group.uuid": "uuid",
        "group.created": "created",
        "group.modified": "modified",
        # Role fields (accessed via related path from Group)
        "role.name": "role_binding_entries__binding__role__name",
        "role.uuid": "role_binding_entries__binding__role__uuid",
        "role.modified": "role_binding_entries__binding__role__modified",
        "role.created": "role_binding_entries__binding__role__created",
        # Annotated in RoleBindingService (assignment time for this resource)
        "last_modified": "latest_modified",
    }

    # Mapping of dot notation fields to Django ORM fields for Principal (user) queryset
    # Note: Principal model doesn't have created/modified fields directly
    USER_FIELD_MAPPING = {
        "user.username": "username",
        "user.uuid": "uuid",
        "role.name": "role_binding_entries__binding__role__name",
        "role.uuid": "role_binding_entries__binding__role__uuid",
        "role.modified": "role_binding_entries__binding__role__modified",
        "role.created": "role_binding_entries__binding__role__created",
        "last_modified": "latest_modified",
    }

    # For role binding list endpoint, the queryset is on RoleBinding model.
    # Values map to annotations defined in RoleBindingQuerySet.for_tenant()
    # so that CursorPagination can extract cursor positions via getattr().
    ROLE_BINDING_FIELD_MAPPING = {
        # Role fields (annotated in RoleBindingQuerySet.for_tenant)
        "role.id": "role_uuid",
        "role.name": "role_name",
        "role.uuid": "role_uuid",
        "role.modified": "role_modified",
        "role.created": "role_created",
        # Resource fields (direct model attributes)
        "resource.id": "resource_id",
        "resource.type": "resource_type",
    }

    # Default ordering for each subject type
    GROUP_DEFAULT_ORDERING = "-modified"
    USER_DEFAULT_ORDERING = "username"
    ROLE_BINDING_DEFAULT_ORDERING = "role_created"

    # Combined mapping for backward compatibility (defaults to group)
    FIELD_MAPPING = GROUP_FIELD_MAPPING

    @staticmethod
    def _is_model(queryset_model, model_name: str) -> bool:
        """Check if queryset model matches expected model by name.

        Uses Django's _meta API for robust comparison that works across
        different import paths and parallel test execution.

        Args:
            queryset_model: The model class from queryset.model
            model_name: Expected model name (e.g., 'RoleBinding', 'Group')

        Returns:
            True if the model matches, False otherwise
        """
        # Use model_name from _meta (Django sets this to class name in lowercase)
        actual_name = queryset_model._meta.model_name
        expected_name = model_name.lower()
        return actual_name == expected_name

    def _get_field_mapping(self, request, queryset) -> dict:
        """Get the appropriate field mapping based on queryset model or subject_type.

        For the list endpoint (RoleBinding queryset), always returns ROLE_BINDING_FIELD_MAPPING
        regardless of any subject_type filter parameter.

        For the by-subject endpoint (Group/Principal queryset), returns the mapping
        based on the subject_type parameter.

        For other endpoints: returns the class's FIELD_MAPPING.

        Args:
            request: The HTTP request object
            queryset: The queryset being paginated

        Returns:
            Field mapping dict appropriate for the endpoint/subject_type
        """
        model = queryset.model

        # Check queryset model first - this ensures correct mapping regardless of
        # query parameters (e.g., list endpoint with subject_type filter)
        if self._is_model(model, "RoleBinding"):
            return self.ROLE_BINDING_FIELD_MAPPING
        if self._is_model(model, "Principal"):
            return self.USER_FIELD_MAPPING
        if self._is_model(model, "Group"):
            return self.GROUP_FIELD_MAPPING

        # Fallback: check subject_type parameter for endpoints with dynamic querysets
        subject_type = request.query_params.get("subject_type")
        if subject_type == "user":
            return self.USER_FIELD_MAPPING
        elif subject_type == "group":
            return self.GROUP_FIELD_MAPPING

        # For endpoints without subject_type, use the class's own FIELD_MAPPING
        return self.FIELD_MAPPING

    def _get_default_ordering(self, request, queryset) -> str:
        """Get the appropriate default ordering based on queryset model or subject_type.

        For the list endpoint (RoleBinding queryset), always uses role_created ordering
        regardless of any subject_type filter parameter.

        For the by-subject endpoint (Group/Principal queryset), uses ordering based
        on the subject_type parameter.

        Args:
            request: The HTTP request object
            queryset: The queryset being paginated

        Returns:
            Default ordering field for the queryset model/subject_type
        """
        model = queryset.model

        # Check queryset model first - this ensures correct ordering regardless of
        # query parameters (e.g., list endpoint with subject_type filter)
        if self._is_model(model, "RoleBinding"):
            return self.ROLE_BINDING_DEFAULT_ORDERING
        if self._is_model(model, "Principal"):
            return self.USER_DEFAULT_ORDERING
        if self._is_model(model, "Group"):
            return self.GROUP_DEFAULT_ORDERING

        # Fallback: check subject_type parameter for endpoints with dynamic querysets
        subject_type = request.query_params.get("subject_type")
        if subject_type == "user":
            return self.USER_DEFAULT_ORDERING
        elif subject_type == "group":
            return self.GROUP_DEFAULT_ORDERING

        # Fall back to instance ordering for backwards compatibility
        return self.ordering

    def _convert_order_field(self, field: str, field_mapping: dict) -> str | None:
        """Convert dot notation field to Django ORM field.

        Accepts any field name present in the field_mapping, including both
        dot notation (e.g., group.name) and direct names (e.g., name).

        Args:
            field: The field name (e.g., group.name, -role.modified)
            field_mapping: The field mapping dict to use for conversion

        Returns:
            The Django ORM field name, or None if the field is invalid
        """
        # Handle descending order prefix
        descending = field.startswith("-")
        field_name = field[1:] if descending else field

        # Check if it's a known mapping
        if field_name in field_mapping:
            orm_field = field_mapping[field_name]
            return f"-{orm_field}" if descending else orm_field

        # Unknown field - reject it
        return None

    def get_ordering(self, request, queryset, view):
        """Get ordering from order_by query parameter or use default.

        Requires dot notation for ordering fields (e.g., group.name, role.name, user.username).
        Direct field names are not allowed. Multiple fields can be specified
        via comma-separated values or multiple order_by parameters.
        Raises ValidationError if invalid ordering is provided.

        Uses the appropriate field mapping based on subject_type:
        - subject_type=user: user.*, role.* fields
        - subject_type=group (or default): group.*, role.* fields
        """
        order_by_list = request.query_params.getlist("order_by")

        # Get appropriate field mapping and default ordering
        field_mapping = self._get_field_mapping(request, queryset)
        default_ordering = self._get_default_ordering(request, queryset)

        # No order_by provided, use default
        if not order_by_list:
            return (default_ordering,)

        # Collect all fields from all order_by parameters (supports both comma-separated and multiple params)
        order_fields = []
        for order_by in order_by_list:
            order_fields.extend([f.strip() for f in order_by.split(",") if f.strip()])

        if not order_fields:
            return (default_ordering,)

        # Convert dot notation to Django ORM fields
        converted_fields = []
        for field in order_fields:
            converted_field = self._convert_order_field(field, field_mapping)
            if converted_field is None:
                valid_fields = ", ".join(sorted(field_mapping.keys()))
                raise ValidationError({"order_by": f"Invalid ordering field '{field}'. Valid fields: {valid_fields}"})
            converted_fields.append(converted_field)

        return tuple(converted_fields)

    @staticmethod
    def link_rewrite(request, link):
        """Rewrite the link based on the path header to only provide partial url."""
        if link is None:
            return None
        url = link
        if PATH_INFO in request.META:
            url_components = urlparse(link)
            path_and_query = url_components.path + (f"?{url_components.query}" if url_components.query else "")
            if bool(re.search("/v[0-9]/", path_and_query)):
                url = path_and_query
            else:
                logger.warning(f"Unable to rewrite link as no version was not found in {path_and_query}.")
        return url

    def get_next_link(self):
        """Create next link with partial url rewrite."""
        next_link = super().get_next_link()
        if next_link is None:
            return next_link
        return V2CursorPagination.link_rewrite(self.request, next_link)

    def get_previous_link(self):
        """Create previous link with partial url rewrite."""
        previous_link = super().get_previous_link()
        if previous_link is None:
            return previous_link
        return V2CursorPagination.link_rewrite(self.request, previous_link)

    def get_paginated_response(self, data):
        """Override pagination output to match V2 API spec.

        Returns different response format only when limit=-1 is explicitly requested.
        All other cases (no limit, limit=10, etc.) use standard V2 pagination format.
        """
        request_limit = self.request.query_params.get(self.page_size_query_param)

        # Special case: when limit=-1 is explicitly requested, return all results
        # with meta.limit set to actual count and no pagination links
        if request_limit == self.NO_LIMIT_ENFORCED_VALUE:
            return Response(
                {
                    "meta": {"limit": len(data)},
                    "links": {
                        "next": None,
                        "previous": None,
                    },
                    "data": data,
                }
            )

        # Default V2 API pagination response for all other cases
        return Response(
            {
                "meta": {"limit": self.get_page_size(self.request)},
                "links": {
                    "next": self.get_next_link(),
                    "previous": self.get_previous_link(),
                },
                "data": data,
            }
        )
