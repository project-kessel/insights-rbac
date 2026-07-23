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

"""Filters for RBAC."""

from django_filters import rest_framework as filters
from management.utils import validate_and_get_key
from rest_framework import serializers
from rest_framework.filters import OrderingFilter

NAME_MATCH_KEY = "name_match"
VALID_NAME_MATCHES = ["partial", "exact"]


class ValidatedOrderingFilter(OrderingFilter):
    """OrderingFilter that validates order_by values and returns 400 for invalid fields."""

    def get_ordering(self, request, queryset, view):
        """Validate ordering fields and return the ordering list."""
        params = request.query_params.get(self.ordering_param)
        if not params:
            return self.get_default_ordering(view)

        fields = [param.strip() for param in params.split(",")]
        valid_fields = self.get_valid_fields(queryset, view, {"request": request})
        valid_field_names = {field[0] for field in valid_fields}

        invalid_fields = []
        for field in fields:
            # Only allow a single optional '-' prefix for descending order
            field_name = field.removeprefix("-")
            if not field_name or field_name not in valid_field_names:
                invalid_fields.append(field)

        if invalid_fields:
            message = "{} query parameter value '{}' is invalid. Valid inputs: {}.".format(
                self.ordering_param, ", ".join(invalid_fields), ", ".join(sorted(valid_field_names))
            )
            raise serializers.ValidationError({self.ordering_param: message})

        return super().get_ordering(request, queryset, view)


class CommonFilters(filters.FilterSet):
    """Common filters."""

    def name_filter(self, queryset, field, value, name_field="name"):
        """Filter to lookup name, partial or exact."""
        match_criteria = validate_and_get_key(self.request.query_params, NAME_MATCH_KEY, VALID_NAME_MATCHES, "partial")

        if match_criteria == "partial":
            return queryset.filter(**{f"{name_field}__icontains": value})
        elif match_criteria == "exact":
            return queryset.filter(**{f"{name_field}__iexact": value})

    def multiple_values_in(self, queryset, field, values):
        """Filter for multiple value lookup."""
        if isinstance(values, str):
            values = values.split(",")

        filters = {f"{field}__in": values}
        return queryset.filter(**filters)
