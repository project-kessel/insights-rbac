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
"""Test the API pagination module."""

from datetime import timedelta
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from api.common.pagination import PATH_INFO, StandardResultsSetPagination, V2CursorPagination, V2ResultsSetPagination


class PaginationTest(TestCase):
    """Tests against the pagination functions."""

    def test_link_rewrite(self):
        """Test the link rewrite."""
        request = Mock()
        request.META = {PATH_INFO: "/v1/providers/"}
        link = "http://localhost:8000/v1/providers/?offset=20"
        expected = "/v1/providers/?offset=20"
        result = StandardResultsSetPagination.link_rewrite(request, link)
        self.assertEqual(expected, result)

    def test_link_rewrite_rbac_prefix(self):
        """Test the link rewrite for RBAC prefix."""
        request = Mock()
        request.META = {PATH_INFO: "/api/rbac/v1/roles/"}
        link = "http://localhost:8000/api/rbac/v1/roles/?limit=10&offset=0"
        expected = "/api/rbac/v1/roles/?limit=10&offset=0"
        result = StandardResultsSetPagination.link_rewrite(request, link)
        self.assertEqual(expected, result)

    def test_link_rewrite_rbac_prefix_v2(self):
        """Test the link rewrite for RBAC prefix for v2 APIs."""
        request = Mock()
        request.META = {PATH_INFO: "/api/rbac/v2/workspaces/"}
        link = "http://localhost:8000/api/rbac/v2/workspaces/?limit=10&offset=0"
        expected = "/api/rbac/v2/workspaces/?limit=10&offset=0"
        result = StandardResultsSetPagination.link_rewrite(request, link)
        self.assertEqual(expected, result)

    def test_link_rewrite_err(self):
        """Test the link rewrite."""
        request = Mock()
        request.META = {PATH_INFO: "https://localhost:8000/providers/"}
        link = "http://localhost:8000/providers/?offset=20"
        result = StandardResultsSetPagination.link_rewrite(request, link)
        self.assertEqual(link, result)

    def test_link_no_rewrite(self):
        """Test the no link rewrite."""
        request = Mock()
        request.META = {}
        link = "http://localhost:8000/api/v1/providers/?offset=20"
        result = StandardResultsSetPagination.link_rewrite(request, link)
        self.assertEqual(link, result)

    @patch("api.common.pagination.LimitOffsetPagination.get_next_link", return_value=None)
    def test_get_next_link_none(self, mock_super):
        """Test the get next link method when super returns none."""
        paginator = StandardResultsSetPagination()
        link = paginator.get_next_link()
        self.assertIsNone(link)

    @patch("api.common.pagination.LimitOffsetPagination.get_previous_link", return_value=None)
    def test_get_previous_link_none(self, mock_super):
        """Test the get previous link method when super returns none."""
        paginator = StandardResultsSetPagination()
        link = paginator.get_previous_link()
        self.assertIsNone(link)

    @patch("api.common.pagination.LimitOffsetPagination.get_next_link")
    def test_get_next_link_value(self, mock_super):
        """Test the get next link method when super returns a value."""
        expected = "http://localhost:8000/api/v1/providers/?offset=20"
        mock_super.return_value = expected
        paginator = StandardResultsSetPagination()
        paginator.request = Mock
        paginator.request.META = {}
        link = paginator.get_next_link()
        self.assertEqual(link, expected)

    @patch("api.common.pagination.LimitOffsetPagination.get_previous_link")
    def test_get_previous_link_value(self, mock_super):
        """Test the get previous link method when super returns a value."""
        expected = "http://localhost:8000/api/v1/providers/?offset=20"
        mock_super.return_value = expected
        paginator = StandardResultsSetPagination()
        paginator.request = Mock
        paginator.request.META = {}
        link = paginator.get_previous_link()
        self.assertEqual(link, expected)


class V2ResultsSetPaginationTest(TestCase):
    """Tests against the V2ResultsSetPagination functions."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.paginator = V2ResultsSetPagination()
        self.users_list_comp = [User.objects.create(username=f"user_{i}") for i in range(100)]
        self.queryset = User.objects.all()

    def test_default_limit(self):
        """Test the default limit."""
        request = Request(self.factory.get("/foo/"))
        paginated_queryset = self.paginator.paginate_queryset(self.queryset, request)
        self.assertEqual(len(paginated_queryset), StandardResultsSetPagination.default_limit)
        self.assertEqual(self.paginator.limit, StandardResultsSetPagination.default_limit)
        self.assertEqual(self.paginator.max_limit, StandardResultsSetPagination.max_limit)

    def test_explicit_limit(self):
        """Test an explicit limit."""
        request = Request(self.factory.get("/foo/?limit=5"))
        paginated_queryset = self.paginator.paginate_queryset(self.queryset, request)
        self.assertEqual(len(paginated_queryset), 5)
        self.assertEqual(self.paginator.limit, 5)
        self.assertEqual(self.paginator.max_limit, StandardResultsSetPagination.max_limit)

    def test_no_limit(self):
        """Test no limit."""
        request = Request(self.factory.get("/foo/?limit=-1"))
        paginated_queryset = self.paginator.paginate_queryset(self.queryset, request)
        self.assertEqual(len(paginated_queryset), self.queryset.count())
        self.assertEqual(self.paginator.limit, self.queryset.count())
        self.assertEqual(self.paginator.max_limit, None)

    def test_empty_queryset(self):
        """Test empty queryset with no limit."""
        request = Request(self.factory.get("/foo/?limit=-1"))
        empty_queryset = User.objects.none()

        paginated_queryset = self.paginator.paginate_queryset(empty_queryset, request)
        self.assertEqual(len(paginated_queryset), 0)
        self.assertEqual(self.paginator.limit, 0)
        self.assertEqual(self.paginator.max_limit, None)


class V2CursorPaginationTest(TestCase):
    """Tests against the V2CursorPagination functions."""

    def setUp(self):
        self.factory = APIRequestFactory()
        self.paginator = V2CursorPagination()
        # Override ordering to use a field that exists on User model
        self.paginator.ordering = "-date_joined"
        self.users_list_comp = [User.objects.create(username=f"cursor_user_{i}") for i in range(25)]
        self.queryset = User.objects.filter(username__startswith="cursor_user_")

    def tearDown(self):
        User.objects.filter(username__startswith="cursor_user_").delete()

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_default_page_size(self, mock_ordering):
        """Test the default page size."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/"))
        paginated_queryset = self.paginator.paginate_queryset(self.queryset, request)
        self.assertEqual(len(paginated_queryset), 10)

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_explicit_limit(self, mock_ordering):
        """Test an explicit limit via page_size_query_param."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?limit=5"))
        paginated_queryset = self.paginator.paginate_queryset(self.queryset, request)
        self.assertEqual(len(paginated_queryset), 5)

    def test_max_page_size(self):
        """Test that max page size is enforced."""
        self.assertEqual(self.paginator.max_page_size, 1000)

    def test_cursor_query_param(self):
        """Test that cursor query param is set correctly."""
        self.assertEqual(self.paginator.cursor_query_param, "cursor")

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_get_paginated_response_structure(self, mock_ordering):
        """Test the paginated response structure."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/"))
        request.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"
        paginated_queryset = self.paginator.paginate_queryset(self.queryset, request)
        response = self.paginator.get_paginated_response([{"id": 1}])

        self.assertIn("meta", response.data)
        self.assertIn("links", response.data)
        self.assertIn("data", response.data)
        self.assertEqual(response.data["meta"]["limit"], 10)
        self.assertIn("next", response.data["links"])
        self.assertIn("previous", response.data["links"])

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_get_paginated_response_uses_actual_limit(self, mock_ordering):
        """Test that paginated response uses the actual limit from request."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?limit=25"))
        request.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"
        self.paginator.paginate_queryset(self.queryset, request)
        response = self.paginator.get_paginated_response([{"id": 1}])

        self.assertEqual(response.data["meta"]["limit"], 25)

    def test_link_rewrite_with_path_info(self):
        """Test link rewrite with PATH_INFO in request META."""
        request = Mock()
        request.META = {PATH_INFO: "/api/rbac/v2/role-bindings/by-subject/"}
        link = "http://localhost:8000/api/rbac/v2/role-bindings/by-subject/?cursor=abc123"
        expected = "/api/rbac/v2/role-bindings/by-subject/?cursor=abc123"
        result = V2CursorPagination.link_rewrite(request, link)
        self.assertEqual(result, expected)

    def test_link_rewrite_without_path_info(self):
        """Test link rewrite without PATH_INFO returns original link."""
        request = Mock()
        request.META = {}
        link = "http://localhost:8000/api/rbac/v2/role-bindings/by-subject/?cursor=abc123"
        result = V2CursorPagination.link_rewrite(request, link)
        self.assertEqual(result, link)

    def test_link_rewrite_with_none_link(self):
        """Test link rewrite with None link returns None."""
        request = Mock()
        request.META = {PATH_INFO: "/api/rbac/v2/role-bindings/by-subject/"}
        result = V2CursorPagination.link_rewrite(request, None)
        self.assertIsNone(result)

    @patch("api.common.pagination.CursorPagination.get_next_link", return_value=None)
    def test_get_next_link_none(self, mock_super):
        """Test get_next_link returns None when super returns None."""
        link = self.paginator.get_next_link()
        self.assertIsNone(link)

    @patch("api.common.pagination.CursorPagination.get_previous_link", return_value=None)
    def test_get_previous_link_none(self, mock_super):
        """Test get_previous_link returns None when super returns None."""
        link = self.paginator.get_previous_link()
        self.assertIsNone(link)

    @patch("api.common.pagination.CursorPagination.get_next_link")
    def test_get_next_link_rewrites(self, mock_super):
        """Test get_next_link rewrites the link."""
        mock_super.return_value = "http://localhost:8000/api/rbac/v2/role-bindings/by-subject/?cursor=xyz"
        self.paginator.request = Mock()
        self.paginator.request.META = {PATH_INFO: "/api/rbac/v2/role-bindings/by-subject/"}
        link = self.paginator.get_next_link()
        self.assertEqual(link, "/api/rbac/v2/role-bindings/by-subject/?cursor=xyz")

    @patch("api.common.pagination.CursorPagination.get_previous_link")
    def test_get_previous_link_rewrites(self, mock_super):
        """Test get_previous_link rewrites the link."""
        mock_super.return_value = "http://localhost:8000/api/rbac/v2/role-bindings/by-subject/?cursor=abc"
        self.paginator.request = Mock()
        self.paginator.request.META = {PATH_INFO: "/api/rbac/v2/role-bindings/by-subject/"}
        link = self.paginator.get_previous_link()
        self.assertEqual(link, "/api/rbac/v2/role-bindings/by-subject/?cursor=abc")

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_empty_queryset(self, mock_ordering):
        """Test pagination with empty queryset."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/"))
        request.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"
        empty_queryset = User.objects.none()

        paginated_queryset = self.paginator.paginate_queryset(empty_queryset, request)
        response = self.paginator.get_paginated_response([])

        self.assertEqual(len(paginated_queryset), 0)
        self.assertEqual(response.data["data"], [])
        self.assertIsNone(response.data["links"]["next"])
        self.assertIsNone(response.data["links"]["previous"])

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_max_page_size_enforced(self, mock_ordering):
        """Test that requests exceeding max_page_size are capped."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?limit=2000"))
        paginated_queryset = self.paginator.paginate_queryset(self.queryset, request)

        # Should be capped to max_page_size (1000) or queryset size if smaller
        self.assertLessEqual(len(paginated_queryset), self.paginator.max_page_size)

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_first_page_has_no_previous(self, mock_ordering):
        """Test that first page has no previous link."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?limit=5"))
        request.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"
        self.paginator.paginate_queryset(self.queryset, request)
        response = self.paginator.get_paginated_response([])

        self.assertIsNone(response.data["links"]["previous"])

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_last_page_has_no_next(self, mock_ordering):
        """Test that last page has no next link."""
        # Request all items in one page
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?limit=100"))
        request.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"
        self.paginator.paginate_queryset(self.queryset, request)
        response = self.paginator.get_paginated_response([])

        self.assertIsNone(response.data["links"]["next"])

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_middle_page_has_both_links(self, mock_ordering):
        """Test that middle pages have both next and previous links."""
        # First, get the first page to obtain cursor for next page
        request1 = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?limit=5"))
        request1.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"
        self.paginator.paginate_queryset(self.queryset, request1)
        response1 = self.paginator.get_paginated_response([])

        # Get the cursor from next link
        next_link = response1.data["links"]["next"]
        self.assertIsNotNone(next_link)

        # Extract cursor from link and make second request
        parsed = urlparse(next_link)
        cursor = parse_qs(parsed.query).get("cursor", [None])[0]
        self.assertIsNotNone(cursor)

        # Request second page
        paginator2 = V2CursorPagination()
        paginator2.ordering = "-date_joined"
        request2 = Request(self.factory.get(f"/api/rbac/v2/role-bindings/by-subject/?limit=5&cursor={cursor}"))
        request2.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"
        paginator2.paginate_queryset(self.queryset, request2)
        response2 = paginator2.get_paginated_response([])

        # Middle page should have both links
        self.assertIsNotNone(response2.data["links"]["previous"])
        self.assertIsNotNone(response2.data["links"]["next"])

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_cursor_navigation_returns_different_results(self, mock_ordering):
        """Test that navigating with cursor returns different results."""
        # Get first page
        paginator1 = V2CursorPagination()
        request1 = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?limit=5"))
        request1.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"
        page1 = paginator1.paginate_queryset(self.queryset, request1)
        page1_ids = [u.id for u in page1]

        # Get cursor for next page
        response1 = paginator1.get_paginated_response([])
        next_link = response1.data["links"]["next"]

        parsed = urlparse(next_link)
        cursor = parse_qs(parsed.query).get("cursor", [None])[0]

        # Get second page
        paginator2 = V2CursorPagination()
        request2 = Request(self.factory.get(f"/api/rbac/v2/role-bindings/by-subject/?limit=5&cursor={cursor}"))
        request2.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"
        page2 = paginator2.paginate_queryset(self.queryset, request2)
        page2_ids = [u.id for u in page2]

        # Pages should have different items
        self.assertEqual(len(set(page1_ids) & set(page2_ids)), 0)

    def test_convert_order_field_rejects_simple_field(self):
        """Test _convert_order_field rejects simple field names without dot notation."""
        field_mapping = self.paginator.GROUP_FIELD_MAPPING
        self.assertIsNone(self.paginator._convert_order_field("name", field_mapping))
        self.assertIsNone(self.paginator._convert_order_field("modified", field_mapping))
        self.assertIsNone(self.paginator._convert_order_field("uuid", field_mapping))

    def test_convert_order_field_rejects_descending_simple_field(self):
        """Test _convert_order_field rejects descending simple field names."""
        field_mapping = self.paginator.GROUP_FIELD_MAPPING
        self.assertIsNone(self.paginator._convert_order_field("-name", field_mapping))
        self.assertIsNone(self.paginator._convert_order_field("-modified", field_mapping))

    def test_convert_order_field_dot_notation_group(self):
        """Test _convert_order_field with group dot notation."""
        field_mapping = self.paginator.GROUP_FIELD_MAPPING
        self.assertEqual(self.paginator._convert_order_field("group.name", field_mapping), "name")
        self.assertEqual(self.paginator._convert_order_field("group.description", field_mapping), "description")
        self.assertEqual(self.paginator._convert_order_field("group.user_count", field_mapping), "principalCount")
        self.assertEqual(self.paginator._convert_order_field("group.uuid", field_mapping), "uuid")
        self.assertEqual(self.paginator._convert_order_field("group.modified", field_mapping), "modified")
        self.assertEqual(self.paginator._convert_order_field("group.created", field_mapping), "created")

    def test_convert_order_field_dot_notation_group_descending(self):
        """Test _convert_order_field with group dot notation and descending prefix."""
        field_mapping = self.paginator.GROUP_FIELD_MAPPING
        self.assertEqual(self.paginator._convert_order_field("-group.name", field_mapping), "-name")
        self.assertEqual(self.paginator._convert_order_field("-group.user_count", field_mapping), "-principalCount")

    def test_convert_order_field_dot_notation_role(self):
        """Test _convert_order_field with role dot notation."""
        field_mapping = self.paginator.GROUP_FIELD_MAPPING
        self.assertEqual(
            self.paginator._convert_order_field("role.name", field_mapping),
            "role_binding_entries__binding__role__name",
        )
        self.assertEqual(
            self.paginator._convert_order_field("role.uuid", field_mapping),
            "role_binding_entries__binding__role__uuid",
        )
        self.assertEqual(
            self.paginator._convert_order_field("-role.modified", field_mapping),
            "-role_binding_entries__binding__role__modified",
        )

    def test_convert_order_field_rejects_unknown_dot_notation(self):
        """Test _convert_order_field rejects unknown dot notation fields."""
        field_mapping = self.paginator.GROUP_FIELD_MAPPING
        self.assertIsNone(self.paginator._convert_order_field("foo.bar", field_mapping))
        self.assertIsNone(self.paginator._convert_order_field("-foo.bar.baz", field_mapping))
        self.assertIsNone(self.paginator._convert_order_field("unknown.field", field_mapping))

    def test_convert_order_field_dot_notation_user(self):
        """Test _convert_order_field with user dot notation."""
        field_mapping = self.paginator.USER_FIELD_MAPPING
        self.assertEqual(self.paginator._convert_order_field("user.username", field_mapping), "username")
        self.assertEqual(self.paginator._convert_order_field("user.uuid", field_mapping), "uuid")
        self.assertEqual(self.paginator._convert_order_field("-user.username", field_mapping), "-username")

    def test_convert_order_field_user_mapping_rejects_group_fields(self):
        """Test _convert_order_field with user mapping rejects group fields."""
        field_mapping = self.paginator.USER_FIELD_MAPPING
        self.assertIsNone(self.paginator._convert_order_field("group.name", field_mapping))
        self.assertIsNone(self.paginator._convert_order_field("group.modified", field_mapping))

    def test_get_ordering_with_group_name(self):
        """Test get_ordering converts group.name to name."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?order_by=group.name"))
        ordering = self.paginator.get_ordering(request, self.queryset, None)
        self.assertEqual(ordering, ("name",))

    def test_get_ordering_with_group_name_descending(self):
        """Test get_ordering converts -group.name to -name."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?order_by=-group.name"))
        ordering = self.paginator.get_ordering(request, self.queryset, None)
        self.assertEqual(ordering, ("-name",))

    def test_get_ordering_with_group_user_count(self):
        """Test get_ordering converts group.user_count to principalCount."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?order_by=group.user_count"))
        ordering = self.paginator.get_ordering(request, self.queryset, None)
        self.assertEqual(ordering, ("principalCount",))

    def test_get_ordering_with_role_name(self):
        """Test get_ordering converts role.name to ORM path."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?order_by=role.name"))
        ordering = self.paginator.get_ordering(request, self.queryset, None)
        self.assertEqual(ordering, ("role_binding_entries__binding__role__name",))

    def test_get_ordering_with_role_name_descending(self):
        """Test get_ordering converts -role.name to descending ORM path."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?order_by=-role.name"))
        ordering = self.paginator.get_ordering(request, self.queryset, None)
        self.assertEqual(ordering, ("-role_binding_entries__binding__role__name",))

    def test_get_ordering_accepts_comma_separated_fields(self):
        """Test get_ordering accepts comma-separated fields."""
        request = Request(
            self.factory.get("/api/rbac/v2/role-bindings/by-subject/?order_by=group.name,-group.modified")
        )
        ordering = self.paginator.get_ordering(request, self.queryset, None)
        self.assertEqual(ordering, ("name", "-modified"))

    def test_get_ordering_accepts_multiple_order_by_params(self):
        """Test get_ordering accepts multiple order_by parameters."""
        request = Request(
            self.factory.get("/api/rbac/v2/role-bindings/by-subject/?order_by=group.name&order_by=-group.modified")
        )
        ordering = self.paginator.get_ordering(request, self.queryset, None)
        self.assertEqual(ordering, ("name", "-modified"))

    def test_get_ordering_rejects_field_not_in_mapping(self):
        """Test get_ordering raises ValidationError for fields not in FIELD_MAPPING."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?order_by=name"))
        with self.assertRaises(ValidationError) as context:
            self.paginator.get_ordering(request, self.queryset, None)
        self.assertIn("order_by", context.exception.detail)

    def test_get_ordering_rejects_unknown_dot_notation(self):
        """Test get_ordering raises ValidationError for unknown dot notation fields."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?order_by=foo.bar"))
        with self.assertRaises(ValidationError) as context:
            self.paginator.get_ordering(request, self.queryset, None)
        self.assertIn("order_by", context.exception.detail)

    def test_get_ordering_rejects_mixed_valid_and_invalid(self):
        """Test get_ordering raises ValidationError when any field is invalid."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?order_by=group.name,invalid_field"))
        with self.assertRaises(ValidationError) as context:
            self.paginator.get_ordering(request, self.queryset, None)
        self.assertIn("order_by", context.exception.detail)

    def test_convert_order_field_user_mapping_role_name(self):
        """Test _convert_order_field with user mapping for role.name uses direct path."""
        field_mapping = self.paginator.USER_FIELD_MAPPING
        self.assertEqual(
            self.paginator._convert_order_field("role.name", field_mapping),
            "role_binding_entries__binding__role__name",
        )

    def test_convert_order_field_user_mapping_role_fields(self):
        """Test _convert_order_field with user mapping for all role fields."""
        field_mapping = self.paginator.USER_FIELD_MAPPING
        self.assertEqual(
            self.paginator._convert_order_field("role.uuid", field_mapping),
            "role_binding_entries__binding__role__uuid",
        )
        self.assertEqual(
            self.paginator._convert_order_field("role.modified", field_mapping),
            "role_binding_entries__binding__role__modified",
        )
        self.assertEqual(
            self.paginator._convert_order_field("role.created", field_mapping),
            "role_binding_entries__binding__role__created",
        )
        self.assertEqual(
            self.paginator._convert_order_field("-role.name", field_mapping),
            "-role_binding_entries__binding__role__name",
        )

    def test_get_ordering_with_user_subject_type_role_name(self):
        """Test get_ordering with subject_type=user converts role.name correctly."""
        request = Request(
            self.factory.get("/api/rbac/v2/role-bindings/by-subject/?subject_type=user&order_by=role.name")
        )
        ordering = self.paginator.get_ordering(request, self.queryset, None)
        self.assertEqual(ordering, ("role_binding_entries__binding__role__name",))

    def test_get_ordering_with_user_subject_type_role_name_descending(self):
        """Test get_ordering with subject_type=user converts -role.name correctly."""
        request = Request(
            self.factory.get("/api/rbac/v2/role-bindings/by-subject/?subject_type=user&order_by=-role.name")
        )
        ordering = self.paginator.get_ordering(request, self.queryset, None)
        self.assertEqual(ordering, ("-role_binding_entries__binding__role__name",))

    def _paginator_with_direct_fields(self):
        """Create a paginator with a FIELD_MAPPING that includes direct field names."""
        paginator = V2CursorPagination()
        paginator.ordering = "name"
        paginator.FIELD_MAPPING = {
            "name": "name",
            "last_modified": "modified",
        }
        return paginator

    def test_convert_order_field_accepts_direct_field_in_mapping(self):
        """Test _convert_order_field accepts direct field names present in FIELD_MAPPING."""
        paginator = self._paginator_with_direct_fields()
        self.assertEqual(paginator._convert_order_field("name", paginator.FIELD_MAPPING), "name")
        self.assertEqual(paginator._convert_order_field("last_modified", paginator.FIELD_MAPPING), "modified")

    def test_convert_order_field_accepts_descending_direct_field(self):
        """Test _convert_order_field accepts descending direct field names in FIELD_MAPPING."""
        paginator = self._paginator_with_direct_fields()
        self.assertEqual(paginator._convert_order_field("-name", paginator.FIELD_MAPPING), "-name")
        self.assertEqual(paginator._convert_order_field("-last_modified", paginator.FIELD_MAPPING), "-modified")

    def test_convert_order_field_rejects_unknown_direct_field(self):
        """Test _convert_order_field rejects direct field names not in FIELD_MAPPING."""
        paginator = self._paginator_with_direct_fields()
        self.assertIsNone(paginator._convert_order_field("uuid", paginator.FIELD_MAPPING))
        self.assertIsNone(paginator._convert_order_field("-description", paginator.FIELD_MAPPING))
        self.assertIsNone(paginator._convert_order_field("group.name", paginator.FIELD_MAPPING))

    def test_get_ordering_with_direct_field_name(self):
        """Test get_ordering accepts direct field names when in FIELD_MAPPING."""
        paginator = self._paginator_with_direct_fields()
        request = Request(self.factory.get("/api/rbac/v2/roles/?order_by=name"))
        ordering = paginator.get_ordering(request, self.queryset, None)
        self.assertEqual(ordering, ("name",))

    def test_get_ordering_with_descending_direct_field(self):
        """Test get_ordering accepts descending direct field names."""
        paginator = self._paginator_with_direct_fields()
        request = Request(self.factory.get("/api/rbac/v2/roles/?order_by=-last_modified"))
        ordering = paginator.get_ordering(request, self.queryset, None)
        self.assertEqual(ordering, ("-modified",))

    def test_get_ordering_with_comma_separated_direct_fields(self):
        """Test get_ordering accepts comma-separated direct field names."""
        paginator = self._paginator_with_direct_fields()
        request = Request(self.factory.get("/api/rbac/v2/roles/?order_by=name,-last_modified"))
        ordering = paginator.get_ordering(request, self.queryset, None)
        self.assertEqual(ordering, ("name", "-modified"))

    def test_get_ordering_rejects_unknown_direct_field(self):
        """Test get_ordering raises ValidationError for direct fields not in FIELD_MAPPING."""
        paginator = self._paginator_with_direct_fields()
        request = Request(self.factory.get("/api/rbac/v2/roles/?order_by=unknown"))
        with self.assertRaises(ValidationError) as context:
            paginator.get_ordering(request, self.queryset, None)
        self.assertIn("order_by", context.exception.detail)

    def test_get_ordering_default_when_no_order_by_with_direct_fields(self):
        """Test get_ordering returns default ordering when order_by is omitted."""
        paginator = self._paginator_with_direct_fields()
        request = Request(self.factory.get("/api/rbac/v2/roles/"))
        ordering = paginator.get_ordering(request, self.queryset, None)
        self.assertEqual(ordering, ("name",))

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_no_limit_returns_all_results(self, mock_ordering):
        """Test that limit=-1 returns all results without pagination."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?limit=-1"))
        request.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"
        paginated_queryset = self.paginator.paginate_queryset(self.queryset, request)

        # Build response from actual paginated_queryset, not hard-coded data
        serialized_data = [{"id": user.id} for user in paginated_queryset]
        response = self.paginator.get_paginated_response(serialized_data)

        # Should return all items
        self.assertEqual(len(paginated_queryset), self.queryset.count())
        # Response data length should match paginated queryset length
        self.assertEqual(len(response.data["data"]), len(paginated_queryset))
        # Meta limit should equal total count
        self.assertEqual(response.data["meta"]["limit"], self.queryset.count())
        # No pagination links
        self.assertIsNone(response.data["links"]["next"])
        self.assertIsNone(response.data["links"]["previous"])

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_no_limit_empty_queryset(self, mock_ordering):
        """Test that limit=-1 handles empty queryset correctly."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?limit=-1"))
        request.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"
        empty_queryset = User.objects.none()

        paginated_queryset = self.paginator.paginate_queryset(empty_queryset, request)

        # Build response from actual paginated_queryset to ensure alignment
        serialized_data = list(paginated_queryset)
        response = self.paginator.get_paginated_response(serialized_data)

        # Should return no items
        self.assertEqual(len(paginated_queryset), 0)
        # Response data length should match paginated queryset length
        self.assertEqual(len(response.data["data"]), len(paginated_queryset))
        # Meta limit should be 0
        self.assertEqual(response.data["meta"]["limit"], 0)
        # No pagination links
        self.assertIsNone(response.data["links"]["next"])
        self.assertIsNone(response.data["links"]["previous"])

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_normal_limit_5_still_works(self, mock_ordering):
        """Test that limit=5 still works with normal pagination."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?limit=5"))
        request.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"
        paginated_queryset = self.paginator.paginate_queryset(self.queryset, request)

        # Build response from actual paginated_queryset
        serialized_data = [{"id": user.id} for user in paginated_queryset]
        response = self.paginator.get_paginated_response(serialized_data)

        # Should return only 5 items (uses normal cursor pagination)
        self.assertEqual(len(paginated_queryset), 5)
        # Response data length should match paginated queryset length
        self.assertEqual(len(response.data["data"]), len(paginated_queryset))
        # Meta limit should be 5
        self.assertEqual(response.data["meta"]["limit"], 5)
        # Should have pagination links (next link exists since we have 25 total)
        self.assertIsNotNone(response.data["links"]["next"])
        # Previous should be None on first page
        self.assertIsNone(response.data["links"]["previous"])

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_normal_limit_20_still_works(self, mock_ordering):
        """Test that limit=20 still works with normal pagination."""
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?limit=20"))
        request.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"
        paginated_queryset = self.paginator.paginate_queryset(self.queryset, request)

        # Build response from actual paginated_queryset
        serialized_data = [{"id": user.id} for user in paginated_queryset]
        response = self.paginator.get_paginated_response(serialized_data)

        # Should return 20 items
        self.assertEqual(len(paginated_queryset), 20)
        # Response data length should match paginated queryset length
        self.assertEqual(len(response.data["data"]), len(paginated_queryset))
        # Meta limit should be 20
        self.assertEqual(response.data["meta"]["limit"], 20)
        # Should have next link since we have 25 total
        self.assertIsNotNone(response.data["links"]["next"])

    @patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined")
    def test_default_limit_still_works(self, mock_ordering):
        """Test that default pagination is NOT overridden when no limit param provided.

        This verifies that limit=-1 only affects behavior when explicitly requested,
        and default pagination (limit=10) is preserved when no limit is specified.
        """
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/"))
        request.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"
        paginated_queryset = self.paginator.paginate_queryset(self.queryset, request)

        # Build response from actual paginated_queryset
        serialized_data = [{"id": user.id} for user in paginated_queryset]
        response = self.paginator.get_paginated_response(serialized_data)

        # Should return default page size (10), NOT all results
        self.assertEqual(len(paginated_queryset), 10)
        # Response data length should match paginated queryset length
        self.assertEqual(len(response.data["data"]), len(paginated_queryset))
        # Meta limit should be 10 (default), NOT len(data)
        self.assertEqual(response.data["meta"]["limit"], 10)
        # Should have pagination links (next link exists since we have 25 total)
        self.assertIsNotNone(response.data["links"]["next"])

    def test_ordering_applied_with_limit_minus_one_default(self):
        """Test that default ordering is applied when limit=-1."""
        # Create users with specific date_joined values to test ordering
        User.objects.filter(username__startswith="order_default_").delete()
        now = timezone.now()

        # Create 5 users with different join dates
        user1 = User.objects.create(username="order_default_oldest", date_joined=now - timedelta(days=5))
        user2 = User.objects.create(username="order_default_old", date_joined=now - timedelta(days=3))
        user3 = User.objects.create(username="order_default_middle", date_joined=now - timedelta(days=2))
        user4 = User.objects.create(username="order_default_new", date_joined=now - timedelta(days=1))
        user5 = User.objects.create(username="order_default_newest", date_joined=now)

        queryset = User.objects.filter(username__startswith="order_default_")

        # Test with default ordering (should use -date_joined from _get_default_ordering)
        paginator = V2CursorPagination()
        request = Request(self.factory.get("/api/rbac/v2/role-bindings/by-subject/?limit=-1"))
        request.META[PATH_INFO] = "/api/rbac/v2/role-bindings/by-subject/"

        with patch.object(V2CursorPagination, "_get_default_ordering", return_value="-date_joined"):
            paginated_queryset = paginator.paginate_queryset(queryset, request)

        # Should return all 5 users
        self.assertEqual(len(paginated_queryset), 5)

        # Should be ordered by date_joined descending (newest first)
        self.assertEqual(paginated_queryset[0].id, user5.id)  # newest
        self.assertEqual(paginated_queryset[1].id, user4.id)
        self.assertEqual(paginated_queryset[2].id, user3.id)
        self.assertEqual(paginated_queryset[3].id, user2.id)
        self.assertEqual(paginated_queryset[4].id, user1.id)  # oldest

        # Cleanup
        User.objects.filter(username__startswith="order_default_").delete()

    def test_ordering_applied_with_limit_minus_one_custom_ascending(self):
        """Test that custom ascending ordering is applied when limit=-1."""
        # Create users with specific usernames to test ordering
        user_c = User.objects.create(username="order_asc_charlie")
        user_a = User.objects.create(username="order_asc_alice")
        user_b = User.objects.create(username="order_asc_bob")

        queryset = User.objects.filter(username__startswith="order_asc_")

        # Test with custom ascending ordering
        paginator = V2CursorPagination()
        paginator.FIELD_MAPPING = {"name": "username"}  # For simple test
        request = Request(self.factory.get("/api/rbac/v2/roles/?limit=-1&order_by=name"))
        request.META[PATH_INFO] = "/api/rbac/v2/roles/"

        paginated_queryset = paginator.paginate_queryset(queryset, request)

        # Should return all 3 users
        self.assertEqual(len(paginated_queryset), 3)

        # Should be ordered by username ascending
        self.assertEqual(paginated_queryset[0].id, user_a.id)  # alice
        self.assertEqual(paginated_queryset[1].id, user_b.id)  # bob
        self.assertEqual(paginated_queryset[2].id, user_c.id)  # charlie

        # Cleanup
        User.objects.filter(username__startswith="order_asc_").delete()

    def test_ordering_applied_with_limit_minus_one_custom_descending(self):
        """Test that custom descending ordering is applied when limit=-1."""
        # Create users with specific usernames to test ordering
        user_c = User.objects.create(username="order_desc_charlie")
        user_a = User.objects.create(username="order_desc_alice")
        user_b = User.objects.create(username="order_desc_bob")

        queryset = User.objects.filter(username__startswith="order_desc_")

        # Test with custom descending ordering
        paginator = V2CursorPagination()
        paginator.FIELD_MAPPING = {"name": "username"}  # For simple test
        request = Request(self.factory.get("/api/rbac/v2/roles/?limit=-1&order_by=-name"))
        request.META[PATH_INFO] = "/api/rbac/v2/roles/"

        paginated_queryset = paginator.paginate_queryset(queryset, request)

        # Should return all 3 users
        self.assertEqual(len(paginated_queryset), 3)

        # Should be ordered by username descending
        self.assertEqual(paginated_queryset[0].id, user_c.id)  # charlie
        self.assertEqual(paginated_queryset[1].id, user_b.id)  # bob
        self.assertEqual(paginated_queryset[2].id, user_a.id)  # alice

        # Cleanup
        User.objects.filter(username__startswith="order_desc_").delete()

    def test_ordering_applied_with_limit_minus_one_multiple_fields(self):
        """Test that multiple order_by fields are applied when limit=-1."""
        # Create users to test multiple field ordering
        # Secondary sort only applies when primary field values are equal
        now = timezone.now()

        # Create users where first name is same, last name differs
        # We'll use email as a tie-breaker since username must be unique
        user1 = User.objects.create(
            username="order_multi_user1", email="alice@example.com", date_joined=now - timedelta(days=3)
        )
        user2 = User.objects.create(
            username="order_multi_user2",
            email="alice@example.com",  # Same email
            date_joined=now - timedelta(days=1),  # Newer
        )
        user3 = User.objects.create(
            username="order_multi_user3", email="bob@example.com", date_joined=now - timedelta(days=2)
        )

        queryset = User.objects.filter(username__startswith="order_multi_")

        # Test with multiple ordering fields: email asc, then date_joined desc
        paginator = V2CursorPagination()
        paginator.FIELD_MAPPING = {"name": "email", "last_modified": "date_joined"}
        request = Request(self.factory.get("/api/rbac/v2/roles/?limit=-1&order_by=name,-last_modified"))
        request.META[PATH_INFO] = "/api/rbac/v2/roles/"

        paginated_queryset = paginator.paginate_queryset(queryset, request)

        # Should return all 3 users
        self.assertEqual(len(paginated_queryset), 3)

        # Should be ordered by email asc, then date_joined desc
        # For same email (alice@example.com), newer user2 comes before older user1
        self.assertEqual(paginated_queryset[0].id, user2.id)  # alice, newer (1 day ago)
        self.assertEqual(paginated_queryset[1].id, user1.id)  # alice, older (3 days ago)
        self.assertEqual(paginated_queryset[2].id, user3.id)  # bob

        # Cleanup
        User.objects.filter(username__startswith="order_multi_").delete()
