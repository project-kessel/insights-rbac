#
# Copyright 2026 Red Hat, Inc.
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
"""Tests for workspace caching at manager and API response levels."""

import uuid
from importlib import reload
from unittest.mock import MagicMock, patch

from django.test.utils import override_settings
from django.urls import clear_url_caches, reverse
from management.cache import WORKSPACE_CACHE, WorkspaceCache, workspace_cache_total
from management.models import Workspace
from management.workspace.service import WorkspaceService
from redis import exceptions as redis_exceptions
from rest_framework import status

from rbac import urls
from tests.identity_request import IdentityRequest
from tests.v2_util import bootstrap_tenant_for_v2_test


@override_settings(ATOMIC_RETRY_DISABLED=True, V2_APIS_ENABLED=True)
class WorkspaceCacheClassTests(IdentityRequest):
    """Tests for the WorkspaceCache class itself."""

    def setUp(self):
        """Set up test fixtures."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
        self.default_workspace = bootstrap_result.default_workspace
        self.root_workspace = bootstrap_result.root_workspace
        self.cache = WorkspaceCache()

    def tearDown(self):
        """Clean up."""
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()

    def test_key_for(self):
        """Cache key follows expected pattern."""
        key = self.cache.key_for("org123", "root")
        self.assertEqual(key, "rbac::workspace::org123::root")

    def test_response_key_for(self):
        """Response cache key follows expected pattern."""
        key = self.cache.response_key_for("org123", "list::root")
        self.assertEqual(key, "rbac::workspace::response::org123::list::root")

    @override_settings(ACCESS_CACHE_ENABLED=False)
    def test_get_workspace_disabled(self):
        """Returns None when ACCESS_CACHE_ENABLED is False."""
        result = self.cache.get_workspace("org123", "root")
        self.assertIsNone(result)

    @override_settings(ACCESS_CACHE_ENABLED=False)
    def test_cache_workspace_disabled(self):
        """No-op when ACCESS_CACHE_ENABLED is False."""
        self.cache.cache_workspace("org123", self.root_workspace)
        # Should not raise, just no-op

    @override_settings(ACCESS_CACHE_ENABLED=False)
    def test_get_response_disabled(self):
        """Returns None when ACCESS_CACHE_ENABLED is False."""
        result = self.cache.get_response("org123", "list::root")
        self.assertIsNone(result)

    @override_settings(ACCESS_CACHE_ENABLED=False)
    def test_cache_response_disabled(self):
        """No-op when ACCESS_CACHE_ENABLED is False."""
        self.cache.cache_response("org123", "list::root", {"data": "test"})
        # Should not raise, just no-op

    @patch.object(WorkspaceCache, "get_from_redis")
    @patch.object(WorkspaceCache, "redis_health_check", return_value=True)
    def test_get_workspace_cache_hit(self, mock_health, mock_get_redis):
        """Cache hit returns workspace from get_cached."""
        self.cache._redis_mocked = False
        self.cache.use_caching = True
        mock_get_redis.return_value = self.root_workspace

        result = self.cache.get_workspace(self.tenant.org_id, "root")
        self.assertIsNotNone(result)
        self.assertEqual(result.id, self.root_workspace.id)
        self.assertEqual(result.type, Workspace.Types.ROOT)

    @patch.object(WorkspaceCache, "get_from_redis", return_value=None)
    @patch.object(WorkspaceCache, "redis_health_check", return_value=True)
    def test_get_workspace_cache_miss(self, mock_health, mock_get_redis):
        """Cache miss returns None."""
        self.cache._redis_mocked = False
        self.cache.use_caching = True
        result = self.cache.get_workspace(self.tenant.org_id, "root")
        self.assertIsNone(result)

    def test_get_response_cache_hit(self):
        """Response cache hit returns JSON data."""
        import json

        self.cache._redis_mocked = False
        test_data = {"meta": {"count": 1}, "data": [{"id": "test"}]}
        mock_conn = MagicMock()
        mock_conn.get.return_value = json.dumps(test_data).encode()

        with patch.object(WorkspaceCache, "redis_health_check", return_value=True):
            with patch.object(type(self.cache), "connection", new_callable=lambda: property(lambda s: mock_conn)):
                result = self.cache.get_response(self.tenant.org_id, "list::root")
                self.assertEqual(result, test_data)

    def test_get_response_cache_miss(self):
        """Response cache miss returns None."""
        self.cache._redis_mocked = False
        mock_conn = MagicMock()
        mock_conn.get.return_value = None

        with patch.object(WorkspaceCache, "redis_health_check", return_value=True):
            with patch.object(type(self.cache), "connection", new_callable=lambda: property(lambda s: mock_conn)):
                result = self.cache.get_response(self.tenant.org_id, "list::root")
                self.assertIsNone(result)

    @patch.object(WorkspaceCache, "redis_health_check", return_value=False)
    def test_get_workspace_redis_down(self, mock_health):
        """Returns None when Redis is unreachable."""
        result = self.cache.get_workspace(self.tenant.org_id, "root")
        self.assertIsNone(result)

    @patch.object(WorkspaceCache, "redis_health_check", return_value=False)
    def test_get_response_redis_down(self, mock_health):
        """Returns None when Redis is unreachable."""
        self.cache._redis_mocked = False
        result = self.cache.get_response(self.tenant.org_id, "list::root")
        self.assertIsNone(result)

    @patch.object(WorkspaceCache, "redis_health_check", side_effect=redis_exceptions.RedisError("connection failed"))
    def test_get_response_redis_error(self, mock_health):
        """Returns None on Redis error."""
        self.cache._redis_mocked = False
        result = self.cache.get_response(self.tenant.org_id, "list::root")
        self.assertIsNone(result)


@override_settings(ATOMIC_RETRY_DISABLED=True, V2_APIS_ENABLED=True)
class WorkspaceManagerCacheTests(IdentityRequest):
    """Tests for workspace manager caching integration (root/default)."""

    def setUp(self):
        """Set up test fixtures."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
        self.default_workspace = bootstrap_result.default_workspace
        self.root_workspace = bootstrap_result.root_workspace

    def tearDown(self):
        """Clean up."""
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()

    @patch.object(WorkspaceCache, "get_workspace", return_value=None)
    @patch.object(WorkspaceCache, "cache_workspace")
    def test_root_cache_miss_populates_cache(self, mock_cache_ws, mock_get_ws):
        """On cache miss, root() fetches from DB and populates cache."""
        result = Workspace.objects.root(tenant=self.tenant)

        self.assertEqual(result.id, self.root_workspace.id)
        mock_get_ws.assert_called_once_with(self.tenant.org_id, Workspace.Types.ROOT)
        mock_cache_ws.assert_called_once_with(self.tenant.org_id, result)

    @patch.object(WorkspaceCache, "get_workspace")
    @patch.object(WorkspaceCache, "cache_workspace")
    def test_root_cache_hit_skips_db(self, mock_cache_ws, mock_get_ws):
        """On cache hit, root() returns cached workspace without DB query."""
        mock_get_ws.return_value = self.root_workspace

        with self.assertNumQueries(0):
            result = Workspace.objects.root(tenant=self.tenant)

        self.assertEqual(result.id, self.root_workspace.id)
        mock_cache_ws.assert_not_called()

    @patch.object(WorkspaceCache, "get_workspace", return_value=None)
    @patch.object(WorkspaceCache, "cache_workspace")
    def test_default_cache_miss_populates_cache(self, mock_cache_ws, mock_get_ws):
        """On cache miss, default() fetches from DB and populates cache."""
        result = Workspace.objects.default(tenant=self.tenant)

        self.assertEqual(result.id, self.default_workspace.id)
        mock_get_ws.assert_called_once_with(self.tenant.org_id, Workspace.Types.DEFAULT)
        mock_cache_ws.assert_called_once_with(self.tenant.org_id, result)

    @patch.object(WorkspaceCache, "get_workspace")
    @patch.object(WorkspaceCache, "cache_workspace")
    def test_default_cache_hit_skips_db(self, mock_cache_ws, mock_get_ws):
        """On cache hit, default() returns cached workspace without DB query."""
        mock_get_ws.return_value = self.default_workspace

        with self.assertNumQueries(0):
            result = Workspace.objects.default(tenant=self.tenant)

        self.assertEqual(result.id, self.default_workspace.id)
        mock_cache_ws.assert_not_called()

    def test_root_with_tenant_id_int_skips_cache(self):
        """When tenant_id is an integer (no org_id), cache is skipped."""
        with patch.object(WorkspaceCache, "get_workspace") as mock_get:
            result = Workspace.objects.root(tenant_id=self.tenant.id)
            self.assertEqual(result.id, self.root_workspace.id)
            mock_get.assert_not_called()


@override_settings(ATOMIC_RETRY_DISABLED=True, V2_APIS_ENABLED=True)
class WorkspaceCacheInvalidationTests(IdentityRequest):
    """Tests for workspace cache invalidation on bootstrap and teardown."""

    def setUp(self):
        """Set up test fixtures."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
        self.default_workspace = bootstrap_result.default_workspace
        self.root_workspace = bootstrap_result.root_workspace

    def tearDown(self):
        """Clean up."""
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()

    @patch.object(WorkspaceCache, "delete_workspaces_for_tenant")
    def test_delete_tenant_invalidates_cache(self, mock_delete):
        """delete_tenant_with_resources invalidates workspace cache."""
        from api.utils import delete_tenant_with_resources

        delete_tenant_with_resources(self.tenant)
        mock_delete.assert_called_once_with(self.tenant.org_id)


@override_settings(ATOMIC_RETRY_DISABLED=True, V2_APIS_ENABLED=True)
class WorkspaceViewCacheTests(IdentityRequest):
    """Tests for workspace view response caching."""

    def setUp(self):
        """Set up test fixtures."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        self.service = WorkspaceService()

        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
        self.default_workspace = bootstrap_result.default_workspace
        self.root_workspace = bootstrap_result.root_workspace

    def tearDown(self):
        """Clean up."""
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()

    @patch.object(WorkspaceCache, "cache_response")
    @patch.object(WorkspaceCache, "get_response", return_value=None)
    def test_list_root_type_populates_cache(self, mock_get, mock_cache):
        """Listing with ?type=root populates the response cache on miss."""
        url = "{}?type=root".format(reverse("v2_management:workspace-list"))
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_get.assert_called_once_with(self.tenant.org_id, "list::root::0::10::name")
        mock_cache.assert_called_once()
        call_args = mock_cache.call_args
        self.assertEqual(call_args[0][0], self.tenant.org_id)
        self.assertEqual(call_args[0][1], "list::root::0::10::name")

    @patch.object(WorkspaceCache, "cache_response")
    @patch.object(WorkspaceCache, "get_response")
    def test_list_root_type_uses_cache_on_hit(self, mock_get, mock_cache):
        """Listing with ?type=root returns cached response when available."""
        cached_data = {"meta": {"count": 1, "limit": 10, "offset": 0}, "links": {}, "data": [{"id": "cached"}]}
        mock_get.return_value = cached_data

        url = "{}?type=root".format(reverse("v2_management:workspace-list"))
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, cached_data)
        mock_cache.assert_not_called()

    @patch.object(WorkspaceCache, "cache_response")
    @patch.object(WorkspaceCache, "get_response", return_value=None)
    def test_list_default_type_populates_cache(self, mock_get, mock_cache):
        """Listing with ?type=default populates the response cache on miss."""
        url = "{}?type=default".format(reverse("v2_management:workspace-list"))
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_get.assert_called_once_with(self.tenant.org_id, "list::default::0::10::name")

    @patch.object(WorkspaceCache, "get_response", return_value=None)
    def test_list_standard_type_not_cached(self, mock_get):
        """Listing with ?type=standard does not use caching."""
        url = "{}?type=standard".format(reverse("v2_management:workspace-list"))
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_get.assert_not_called()

    @patch.object(WorkspaceCache, "get_response", return_value=None)
    def test_list_with_name_filter_not_cached(self, mock_get):
        """Listing with additional filters besides type does not use caching."""
        url = "{}?type=root&name=test".format(reverse("v2_management:workspace-list"))
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_get.assert_not_called()

    @patch.object(WorkspaceCache, "get_response", return_value=None)
    def test_list_all_types_not_cached(self, mock_get):
        """Listing without type filter does not use caching."""
        url = reverse("v2_management:workspace-list")
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_get.assert_not_called()

    @patch.object(WorkspaceCache, "get_response", return_value=None)
    def test_list_multiple_types_not_cached(self, mock_get):
        """Listing with multiple type values does not use caching."""
        url = "{}?type=root,default".format(reverse("v2_management:workspace-list"))
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_get.assert_not_called()

    @patch.object(WorkspaceCache, "cache_response")
    @patch.object(WorkspaceCache, "get_response", return_value=None)
    def test_retrieve_builtin_populates_cache(self, mock_get, mock_cache):
        """Retrieving a built-in workspace populates the response cache."""
        pk = str(self.root_workspace.id)
        url = reverse("v2_management:workspace-detail", kwargs={"pk": pk})
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_get.assert_called_once_with(self.tenant.org_id, f"retrieve::{pk}::ancestry=false")
        mock_cache.assert_called_once()
        call_args = mock_cache.call_args
        self.assertEqual(call_args[0][1], f"retrieve::{pk}::ancestry=false")

    @patch.object(WorkspaceCache, "cache_response")
    @patch.object(WorkspaceCache, "get_response")
    def test_retrieve_builtin_cache_hit(self, mock_get, mock_cache):
        """Retrieving a built-in workspace returns cached response on hit."""
        cached_data = {"id": str(self.root_workspace.id), "name": "Root Workspace", "type": "root"}
        mock_get.return_value = cached_data

        url = reverse("v2_management:workspace-detail", kwargs={"pk": str(self.root_workspace.id)})
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, cached_data)
        mock_cache.assert_not_called()

    @patch.object(WorkspaceCache, "cache_response")
    @patch.object(WorkspaceCache, "get_response", return_value=None)
    def test_retrieve_standard_not_cached(self, mock_get, mock_cache):
        """Retrieving a standard workspace does not populate the response cache."""
        standard_ws = self.service.create(
            {"name": "Standard Test", "description": "Test", "parent_id": self.default_workspace.id},
            self.tenant,
        )
        url = reverse("v2_management:workspace-detail", kwargs={"pk": str(standard_ws.id)})
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_cache.assert_not_called()

    @patch.object(WorkspaceCache, "cache_response")
    @patch.object(WorkspaceCache, "get_response", return_value=None)
    def test_retrieve_404_not_cached(self, mock_get, mock_cache):
        """Retrieving a non-existent workspace does not populate the response cache."""
        fake_pk = str(uuid.uuid4())
        url = reverse("v2_management:workspace-detail", kwargs={"pk": fake_pk})
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        mock_cache.assert_not_called()

    @patch.object(WorkspaceCache, "get_response", return_value=None)
    def test_list_with_parent_id_filter_not_cached(self, mock_get):
        """Listing with parent_id filter does not use caching."""
        url = "{}?type=root&parent_id={}".format(reverse("v2_management:workspace-list"), str(self.root_workspace.id))
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_get.assert_not_called()

    @patch.object(WorkspaceCache, "get_response", return_value=None)
    def test_list_with_ids_filter_not_cached(self, mock_get):
        """Listing with ids filter does not use caching."""
        url = "{}?type=root&ids={}".format(reverse("v2_management:workspace-list"), str(self.root_workspace.id))
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_get.assert_not_called()

    @patch.object(WorkspaceCache, "cache_response")
    @patch.object(WorkspaceCache, "get_response", return_value=None)
    def test_list_different_pagination_different_cache_key(self, mock_get, mock_cache):
        """Listing with explicit pagination uses pagination-aware cache key."""
        url = "{}?type=root&offset=5&limit=20".format(reverse("v2_management:workspace-list"))
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_get.assert_called_once_with(self.tenant.org_id, "list::root::5::20::name")

    @patch.object(WorkspaceCache, "cache_response")
    @patch.object(WorkspaceCache, "get_response", return_value=None)
    def test_retrieve_with_ancestry_uses_different_cache_key(self, mock_get, mock_cache):
        """Retrieve with include_ancestry=true uses a different cache key than without."""
        pk = str(self.root_workspace.id)
        url = "{}?include_ancestry=true".format(reverse("v2_management:workspace-detail", kwargs={"pk": pk}))
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_get.assert_called_once_with(self.tenant.org_id, f"retrieve::{pk}::ancestry=true")
        mock_cache.assert_called_once()
        call_args = mock_cache.call_args
        self.assertEqual(call_args[0][1], f"retrieve::{pk}::ancestry=true")

    @patch.object(WorkspaceCache, "cache_response")
    @patch.object(WorkspaceCache, "get_response", return_value=None)
    def test_list_different_order_by_different_cache_key(self, mock_get, mock_cache):
        """Listing with different order_by uses order-aware cache key."""
        url = "{}?type=root&order_by=-name".format(reverse("v2_management:workspace-list"))
        response = self.client.get(url, **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_get.assert_called_once_with(self.tenant.org_id, "list::root::0::10::-name")


@override_settings(ATOMIC_RETRY_DISABLED=True, V2_APIS_ENABLED=True)
class WorkspaceCacheMetricsTests(IdentityRequest):
    """Tests for workspace cache Prometheus metrics."""

    def setUp(self):
        """Set up test fixtures."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.tenant.save()

        bootstrap_result = bootstrap_tenant_for_v2_test(self.tenant)
        self.default_workspace = bootstrap_result.default_workspace
        self.root_workspace = bootstrap_result.root_workspace
        self.cache = WorkspaceCache()

    def tearDown(self):
        """Clean up."""
        Workspace.objects.update(parent=None)
        Workspace.objects.all().delete()

    @patch.object(WorkspaceCache, "get_from_redis", return_value=None)
    @patch.object(WorkspaceCache, "redis_health_check", return_value=True)
    def test_get_workspace_miss_increments_metric(self, mock_health, mock_get_redis):
        """Cache miss on get_workspace increments model miss counter."""
        self.cache._redis_mocked = False
        self.cache.use_caching = True
        before = workspace_cache_total.labels(cache_layer="model", result="miss")._value.get()
        self.cache.get_workspace(self.tenant.org_id, "root")
        after = workspace_cache_total.labels(cache_layer="model", result="miss")._value.get()
        self.assertEqual(after - before, 1)

    @patch.object(WorkspaceCache, "get_from_redis")
    @patch.object(WorkspaceCache, "redis_health_check", return_value=True)
    def test_get_workspace_hit_increments_metric(self, mock_health, mock_get_redis):
        """Cache hit on get_workspace increments model hit counter."""
        self.cache._redis_mocked = False
        self.cache.use_caching = True
        mock_get_redis.return_value = self.root_workspace
        before = workspace_cache_total.labels(cache_layer="model", result="hit")._value.get()
        self.cache.get_workspace(self.tenant.org_id, "root")
        after = workspace_cache_total.labels(cache_layer="model", result="hit")._value.get()
        self.assertEqual(after - before, 1)

    def test_get_response_miss_increments_metric(self):
        """Cache miss on get_response increments response miss counter."""
        self.cache._redis_mocked = False
        mock_conn = MagicMock()
        mock_conn.get.return_value = None

        with patch.object(WorkspaceCache, "redis_health_check", return_value=True):
            with patch.object(type(self.cache), "connection", new_callable=lambda: property(lambda s: mock_conn)):
                before = workspace_cache_total.labels(cache_layer="response", result="miss")._value.get()
                self.cache.get_response(self.tenant.org_id, "list::root")
                after = workspace_cache_total.labels(cache_layer="response", result="miss")._value.get()
                self.assertEqual(after - before, 1)

    def test_get_response_hit_increments_metric(self):
        """Cache hit on get_response increments response hit counter."""
        import json

        self.cache._redis_mocked = False
        test_data = {"meta": {"count": 1}, "data": [{"id": "test"}]}
        mock_conn = MagicMock()
        mock_conn.get.return_value = json.dumps(test_data).encode()

        with patch.object(WorkspaceCache, "redis_health_check", return_value=True):
            with patch.object(type(self.cache), "connection", new_callable=lambda: property(lambda s: mock_conn)):
                before = workspace_cache_total.labels(cache_layer="response", result="hit")._value.get()
                self.cache.get_response(self.tenant.org_id, "list::root")
                after = workspace_cache_total.labels(cache_layer="response", result="hit")._value.get()
                self.assertEqual(after - before, 1)
