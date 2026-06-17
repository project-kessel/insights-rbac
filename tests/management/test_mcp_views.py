#
# Copyright 2025 Red Hat, Inc.
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
"""Test the MCP views via _private/_a2s/ path."""

import inspect
import json
import time
from importlib import reload
from unittest.mock import patch

from django.test import override_settings
from django.urls import clear_url_caches, reverse
from django.utils import timezone
from management.mcp_views import (
    ApiVersion,
    ToolConfig,
    ToolTimeoutError,
    _TOOL_CONFIG,
    _check_kessel_access,
    _check_v1_access,
    _execute_with_timeout,
    _permission_matches,
    _sanitize_validation_error,
    _validate_tool_arguments,
)
from management.models import Access, AuditLog, Group, Permission, Policy, Principal, Role
from management.workspace.model import Workspace
from management.relation_replicator.noop_replicator import NoopReplicator
from management.role.v2_model import RoleV2
from management.role_binding.model import RoleBinding, RoleBindingGroup, RoleBindingPrincipal
from management.tenant_mapping.model import TenantMapping
from management.tenant_service.v2 import V2TenantBootstrapService
from rest_framework import status
from rest_framework.test import APIClient
from tests.identity_request import IdentityRequest

from api.models import CrossAccountRequest, Tenant
from rbac import urls


class MCPToolTestMixin:
    """Shared helpers for calling MCP tools in tests."""

    def setUp(self):
        """Auto-mock access checks so protected tools are callable in tests."""
        super().setUp()
        kessel_patcher = patch("management.mcp_views._check_kessel_access", return_value=True)
        self._kessel_mock = kessel_patcher.start()
        self.addCleanup(kessel_patcher.stop)
        v1_patcher = patch("management.mcp_views._check_v1_access", return_value=True)
        self._v1_access_mock = v1_patcher.start()
        self.addCleanup(v1_patcher.stop)

    def _call_tool(self, tool_name, arguments=None, use_auth=True):
        """Helper to call an MCP tool and return the parsed response."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 100,
            "params": {"name": tool_name, "arguments": arguments or {}},
        }
        kwargs = {"data": json.dumps(body), "content_type": "application/json"}
        if use_auth:
            kwargs.update(self.headers)
        return self.client.post(self.url, **kwargs)

    def _get_tool_names(self):
        """Get tool names from tools/list response."""
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)
        return [t["name"] for t in response.json()["result"]["tools"]]

    def _get_tool_output(self, response):
        """Extract tool output from MCP response."""
        data = response.json()
        return json.loads(data["result"]["content"][0]["text"])


class MCPViewTests(MCPToolTestMixin, IdentityRequest):
    """Test the MCP endpoint at /_private/_a2s/mcp/ (agent-to-service with public auth)."""

    def setUp(self):
        """Set up the MCP view tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)

    def tearDown(self):
        """Tear down MCP view tests."""
        AuditLog.objects.all().delete()
        Policy.objects.all().delete()
        Role.objects.all().delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    # --- Protocol tests ---

    def test_initialize_returns_server_info(self):
        """Positive: MCP initialize returns server capabilities."""
        body = {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["jsonrpc"], "2.0")
        self.assertEqual(data["id"], 1)
        result = data["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "RBAC")
        self.assertIn("Mcp-Session-Id", response)

    def test_initialize_returns_instructions(self):
        """Positive: MCP initialize includes instructions field."""
        body = {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        result = response.json()["result"]
        self.assertIn("instructions", result)
        self.assertIn("RBAC", result["instructions"])

    def test_initialize_instructions_always_include_remediation_guidance(self):
        """Positive: instructions always include remediation guidance regardless of write mode."""
        body = {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        instructions = response.json()["result"]["instructions"]
        self.assertIn("Remediation Guidance", instructions)
        self.assertIn("Permission gaps", instructions)
        self.assertIn("numbered options", instructions)

    @override_settings(MCP_WRITE_ENABLED=True)
    def test_initialize_instructions_include_write_actions_when_writes_enabled(self):
        """Positive: instructions include write action prompt when write mode is on."""
        body = {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        instructions = response.json()["result"]["instructions"]
        self.assertIn("Write Actions", instructions)
        self.assertIn("NEVER execute a write tool", instructions)

    @override_settings(MCP_WRITE_ENABLED=False)
    def test_initialize_instructions_omit_write_actions_when_writes_disabled(self):
        """Negative: instructions omit write action prompt when write mode is off."""
        body = {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        instructions = response.json()["result"]["instructions"]
        self.assertNotIn("Write Actions", instructions)
        self.assertIn("Remediation Guidance", instructions)

    def test_initialize_instructions_include_honest_caveats(self):
        """Positive: instructions always include cross-cutting honest caveats."""
        body = {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        instructions = response.json()["result"]["instructions"]
        self.assertIn("Honest Caveats", instructions)
        self.assertIn("ResourceDefinition filters are opaque", instructions)
        self.assertIn("Org admins have implicit full access", instructions)
        self.assertIn("Permission-to-UI mapping does not exist", instructions)
        self.assertIn("V1 vs V2 role assignment model", instructions)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": [{"username": "caveat_user", "email": "c@example.com"}],
            "userCount": 1,
        },
    )
    def test_tool_result_includes_caveats_in_separate_content_block(self, mock_request):
        """Positive: tools with caveats append a second content block with caveat text."""
        response = self._call_tool("list_principals", {"limit": 1})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result = response.json()["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(len(result["content"]), 2)

        json.loads(result["content"][0]["text"])

        caveat_block = result["content"][1]
        self.assertEqual(caveat_block["type"], "text")
        self.assertIn("IMPORTANT", caveat_block["text"])
        self.assertIn("CAVEATS", caveat_block["text"])
        self.assertIn("is_org_admin", caveat_block["text"])

    def test_tool_result_without_caveats_has_single_content_block(self):
        """Positive: tools without caveats return only one content block."""
        response = self._call_tool("hello", {"message": "test"}, use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result = response.json()["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["type"], "text")

        tool_output = json.loads(result["content"][0]["text"])
        self.assertIn("response", tool_output)

    def test_notification_returns_202(self):
        """Positive: JSON-RPC notification (no id) returns 202."""
        body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, 202)

    def test_tools_list_returns_list_principals_tool(self):
        """Positive: tools/list returns the list_principals tool with schema."""
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        tools = data["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        self.assertIn("list_principals", tool_names)

        tool = next(t for t in tools if t["name"] == "list_principals")
        self.assertIn("inputSchema", tool)
        schema = tool["inputSchema"]
        self.assertIn("limit", schema["properties"])
        self.assertIn("offset", schema["properties"])
        self.assertIn("sort_order", schema["properties"])

    def test_unknown_method_returns_error(self):
        """Negative: Unknown method returns -32601 error."""
        body = {"jsonrpc": "2.0", "method": "unknown/method", "id": 3, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertNotIn("result", data)
        self.assertEqual(data["error"]["code"], -32601)
        self.assertIn("Method not found", data["error"]["message"])

    def test_invalid_json_returns_parse_error(self):
        """Negative: Invalid JSON returns -32700 parse error."""
        response = self.client.post(self.url, data="not json", content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertNotIn("result", data)
        self.assertEqual(data["error"]["code"], -32700)

    def test_invalid_jsonrpc_version_returns_error(self):
        """Negative: Wrong jsonrpc version returns -32600 error."""
        body = {"jsonrpc": "1.0", "method": "initialize", "id": 4, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertNotIn("result", data)
        self.assertEqual(data["error"]["code"], -32600)

    def test_missing_method_returns_error(self):
        """Negative: Missing method field returns -32600 error."""
        body = {"jsonrpc": "2.0", "id": 40, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertNotIn("result", data)
        self.assertEqual(data["error"]["code"], -32600)
        self.assertIn("method", data["error"]["message"])

    def test_notification_without_method_returns_error(self):
        """Negative: Notification (no id) without method returns -32600 error."""
        body = {"jsonrpc": "2.0"}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertNotIn("result", data)
        self.assertEqual(data["error"]["code"], -32600)

    def test_get_returns_405(self):
        """Negative: GET request returns 405 (SSE not supported in WSGI)."""
        response = self.client.get(self.url, **self.headers)
        self.assertEqual(response.status_code, 405)

    def test_delete_returns_200(self):
        """Positive: DELETE for session termination returns 200."""
        response = self.client.delete(self.url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_tools_list_returns_hello_tool(self):
        """Positive: tools/list includes the hello tool with message param."""
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tools = response.json()["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        self.assertIn("hello", tool_names)

        hello_tool = next(t for t in tools if t["name"] == "hello")
        self.assertIn("message", hello_tool["inputSchema"]["properties"])

    # --- Tool execution tests ---

    def test_tools_call_hello_with_default(self):
        """Positive: hello tool returns date and default message."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 20,
            "params": {"name": "hello", "arguments": {}},
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        result = data["result"]
        self.assertFalse(result["isError"])
        tool_output = json.loads(result["content"][0]["text"])
        self.assertEqual(tool_output["response"], "RBAC received your message: 'Hello, World!'")
        self.assertIn("date", tool_output)

    def test_tools_call_hello_with_custom_message(self):
        """Positive: hello tool echoes back custom message."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 21,
            "params": {"name": "hello", "arguments": {"message": "Hi from LLM!"}},
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = json.loads(response.json()["result"]["content"][0]["text"])
        self.assertEqual(tool_output["response"], "RBAC received your message: 'Hi from LLM!'")
        self.assertRegex(tool_output["date"], r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC")

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": [{"username": "test_user", "email": "test@example.com"}],
            "userCount": 1,
        },
    )
    def test_tools_call_list_principals_success(self, mock_request):
        """Positive: tools/call list_principals returns principal data via PrincipalView."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 5,
            "params": {
                "name": "list_principals",
                "arguments": {"limit": 10, "offset": 0},
            },
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["id"], 5)
        result = data["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][0]["type"], "text")

        tool_output = json.loads(result["content"][0]["text"])
        self.assertEqual(tool_output["meta"]["count"], 1)
        self.assertIn("links", tool_output)
        self.assertEqual(len(tool_output["data"]), 1)
        self.assertNotIn("email", tool_output["data"][0])
        self.assertNotIn("last_name", tool_output["data"][0])

        self.assertEqual(len(result["content"]), 2)
        self.assertIn("IMPORTANT", result["content"][1]["text"])
        self.assertIn("CAVEATS", result["content"][1]["text"])

    @patch("management.mcp_views._principal_view")
    def test_tools_call_list_principals_non_drf_response(self, mock_principal_view):
        """Positive: tools/call handles plain HttpResponse (non-DRF) via content.decode() fallback."""
        from django.http import HttpResponse as DjangoHttpResponse

        payload = {"data": [{"username": "plain_user"}], "meta": {"count": 1}}
        mock_principal_view.return_value = DjangoHttpResponse(
            json.dumps(payload), content_type="application/json", status=200
        )

        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 50,
            "params": {"name": "list_principals", "arguments": {}},
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["id"], 50)
        result = data["result"]
        self.assertFalse(result["isError"])
        tool_output = json.loads(result["content"][0]["text"])
        self.assertIn("username", tool_output["data"][0])

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": {"userCount": "2", "users": [{"username": "user1"}, {"username": "user2"}]},
        },
    )
    def test_tools_call_list_principals_dict_response(self, mock_request):
        """Positive: tools/call handles dict-style proxy response."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 6,
            "params": {
                "name": "list_principals",
                "arguments": {"limit": 5, "offset": 0, "sort_order": "desc"},
            },
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        tool_output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(tool_output["meta"]["count"], "2")
        self.assertEqual(len(tool_output["data"]), 2)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": [{"username": "test_user"}],
            "userCount": 1,
        },
    )
    def test_tools_call_passes_options_to_proxy(self, mock_request):
        """Positive: tools/call passes correct options to PrincipalProxy via PrincipalView."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 7,
            "params": {
                "name": "list_principals",
                "arguments": {
                    "limit": 20,
                    "offset": 5,
                    "sort_order": "desc",
                    "status": "disabled",
                    "username_only": "true",
                },
            },
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_request.assert_called_once_with(
            org_id=self.customer_data["org_id"],
            limit=20,
            offset=5,
            options={
                "limit": 20,
                "offset": 5,
                "sort_order": "desc",
                "status": "disabled",
                "username_only": "true",
                "principal_type": "user",
                "admin_only": "false",
            },
        )

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": [
                {"username": "jsmith", "first_name": "John", "last_name": "Smith", "email": "jsmith@example.com"},
                {"username": "jdoe", "first_name": "Jane", "last_name": "Doe", "email": "jdoe@example.com"},
                {
                    "username": "rbac_user",
                    "first_name": "RBAC",
                    "last_name": "Normal For V2",
                    "email": "rbac@example.com",
                },
            ],
            "userCount": 3,
        },
    )
    def test_tools_call_list_principals_filter_by_name_partial(self, mock_request):
        """Positive: list_principals filters by name with partial match (e.g., 'RBAC Normal' matches 'RBAC Normal For V2')."""
        response = self._call_tool("list_principals", {"name": "RBAC Normal"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["meta"]["count"], 1)
        self.assertEqual(len(tool_output["data"]), 1)
        self.assertNotIn("email", tool_output["data"][0])
        self.assertNotIn("last_name", tool_output["data"][0])

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": [
                {"username": "jsmith", "first_name": "John", "last_name": "Smith", "email": "jsmith@example.com"},
                {"username": "jdoe", "first_name": "Jane", "last_name": "Doe", "email": "jdoe@example.com"},
                {"username": "asmith", "first_name": "Alice", "last_name": "Smith", "email": "asmith@example.com"},
            ],
            "userCount": 3,
        },
    )
    def test_tools_call_list_principals_filter_by_name_multiple_matches(self, mock_request):
        """Positive: list_principals name filter returns all matching users."""
        response = self._call_tool("list_principals", {"name": "Smith"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["meta"]["count"], 2)
        self.assertEqual(len(tool_output["data"]), 2)
        for user in tool_output["data"]:
            self.assertNotIn("email", user)
            self.assertNotIn("last_name", user)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": [
                {"username": "jsmith", "first_name": "John", "last_name": "Smith", "email": "jsmith@example.com"},
                {"username": "jdoe", "first_name": "Jane", "last_name": "Doe", "email": "jdoe@example.com"},
            ],
            "userCount": 2,
        },
    )
    def test_tools_call_list_principals_filter_by_name_case_insensitive(self, mock_request):
        """Positive: list_principals name filter is case-insensitive."""
        response = self._call_tool("list_principals", {"name": "john smith"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["meta"]["count"], 1)
        self.assertEqual(len(tool_output["data"]), 1)
        self.assertNotIn("email", tool_output["data"][0])
        self.assertNotIn("last_name", tool_output["data"][0])

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": [
                {"username": "jsmith", "first_name": "John", "last_name": "Smith", "email": "jsmith@example.com"},
            ],
            "userCount": 1,
        },
    )
    def test_tools_call_list_principals_filter_by_name_no_match(self, mock_request):
        """Positive: list_principals returns empty when name filter has no matches."""
        response = self._call_tool("list_principals", {"name": "NonExistent"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["meta"]["count"], 0)
        self.assertEqual(len(tool_output["data"]), 0)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": [
                {"username": "jsmith", "first_name": "John", "last_name": "Smith", "email": "jsmith@example.com"},
                {"username": "asmith", "first_name": "Alice", "last_name": "Smith", "email": "asmith@example.com"},
            ],
            "userCount": 2,
        },
    )
    def test_tools_call_list_principals_filter_by_name_username_only(self, mock_request):
        """Positive: list_principals with name filter and username_only returns only usernames."""
        response = self._call_tool("list_principals", {"name": "Smith", "username_only": "true"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["meta"]["count"], 2)
        self.assertEqual(len(tool_output["data"]), 2)
        for user in tool_output["data"]:
            self.assertIn("username", user)
            self.assertNotIn("email", user)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 500,
            "errors": [{"detail": "BOP service unavailable", "status": "500", "source": "principals"}],
        },
    )
    def test_tools_call_list_principals_filter_by_name_bop_error(self, mock_request):
        """Negative: list_principals with name filter handles BOP error gracefully."""
        response = self._call_tool("list_principals", {"name": "Smith"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertIn("errors", tool_output)
        self.assertEqual(tool_output["errors"][0]["detail"], "BOP service unavailable")

    def test_tools_call_list_principals_name_and_usernames_conflict(self):
        """Negative: list_principals rejects conflicting name and usernames parameters."""
        response = self._call_tool("list_principals", {"name": "Smith", "usernames": "jsmith"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertIn("errors", tool_output)
        self.assertEqual(tool_output["errors"][0]["detail"], "Cannot use both 'name' and 'usernames' parameters")

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 500,
            "errors": [{"detail": "Unexpected error.", "status": "500", "source": "principals"}],
        },
    )
    def test_tools_call_list_principals_proxy_error(self, mock_request):
        """Negative: tools/call handles proxy error gracefully via PrincipalView."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 8,
            "params": {
                "name": "list_principals",
                "arguments": {},
            },
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertNotIn("error", data)
        self.assertFalse(data["result"]["isError"])
        tool_output = json.loads(data["result"]["content"][0]["text"])
        self.assertIn("errors", tool_output)

    @patch("management.mcp_views._call_view")
    def test_tools_call_list_principals_username_not_found_hint(self, mock_call_view):
        """Positive: list_principals includes retry hint when usernames lookup returns zero results."""
        mock_call_view.return_value = json.dumps({"meta": {"count": 0}, "data": [], "links": {}})
        response = self._call_tool("list_principals", {"usernames": "Joe"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["meta"]["count"], 0)
        self.assertIn("hint", tool_output)
        self.assertIn("Joe", tool_output["hint"])
        self.assertIn("list_principals(name='Joe')", tool_output["hint"])

    @patch("management.mcp_views._call_view")
    def test_tools_call_list_principals_username_found_no_hint(self, mock_call_view):
        """Positive: list_principals does NOT include hint when usernames lookup finds results."""
        mock_call_view.return_value = json.dumps(
            {"meta": {"count": 1}, "data": [{"username": "joe_user"}], "links": {}}
        )
        response = self._call_tool("list_principals", {"usernames": "joe_user"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["meta"]["count"], 1)
        self.assertNotIn("hint", tool_output)

    @patch("management.mcp_views._call_view")
    def test_tools_call_list_principals_no_hint_without_usernames(self, mock_call_view):
        """Positive: list_principals does NOT include hint when no usernames filter is used."""
        mock_call_view.return_value = json.dumps({"meta": {"count": 0}, "data": [], "links": {}})
        response = self._call_tool("list_principals", {"limit": 10})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertNotIn("hint", tool_output)

    @override_settings(MCP_PII_REDACTION_ENABLED=False)
    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={
            "status_code": 200,
            "data": [
                {"username": "test_user", "email": "test@example.com", "first_name": "Test", "last_name": "User"}
            ],
            "userCount": 1,
        },
    )
    def test_list_principals_pii_visible_when_redaction_disabled(self, mock_request):
        """Positive: PII fields are returned when PII redaction is disabled via env var."""
        response = self._call_tool("list_principals", {"limit": 10})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["data"][0]["username"], "test_user")
        self.assertEqual(tool_output["data"][0]["email"], "test@example.com")

    def test_tools_call_unknown_tool_returns_error(self):
        """Negative: Calling an unknown tool returns error."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 9,
            "params": {
                "name": "nonexistent_tool",
                "arguments": {},
            },
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertNotIn("result", data)
        self.assertEqual(data["error"]["code"], -32602)
        self.assertEqual(data["error"]["message"], "Unknown tool: nonexistent_tool")

    @patch.dict(
        "management.mcp_views._TOOL_CONFIG",
        {"hello": ToolConfig(fn=lambda **kw: (_ for _ in ()).throw(Exception("boom")))},
    )
    def test_tools_call_internal_error_returns_32603(self):
        """Negative: tool raising an exception returns JSON-RPC -32603."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 30,
            "params": {"name": "hello", "arguments": {}},
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertNotIn("result", data)
        self.assertEqual(data["error"]["code"], -32603)

    def test_non_dict_params_returns_invalid_request(self):
        """Negative: params as a list instead of object returns -32600."""
        body = {"jsonrpc": "2.0", "method": "tools/call", "id": 32, "params": [1, 2, 3]}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertNotIn("result", data)
        self.assertEqual(data["error"]["code"], -32600)
        self.assertIn("params must be an object", data["error"]["message"])

    def test_tools_call_missing_name_returns_unknown_tool(self):
        """Negative: tools/call without name returns unknown tool error."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 34,
            "params": {"arguments": {}},
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertNotIn("result", data)
        self.assertEqual(data["error"]["code"], -32602)
        self.assertIn("Unknown tool", data["error"]["message"])

    def test_tools_call_non_dict_arguments_returns_32602(self):
        """Negative: tools/call with non-dict arguments returns -32602."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 35,
            "params": {"name": "hello", "arguments": "not a dict"},
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertNotIn("result", data)
        self.assertEqual(data["error"]["code"], -32602)
        self.assertIn("arguments must be an object", data["error"]["message"])

    def test_tools_call_missing_arguments_returns_32602(self):
        """Negative: tools/call without arguments field returns -32602."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 33,
            "params": {"name": "hello"},
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertNotIn("result", data)
        self.assertEqual(data["error"]["code"], -32602)
        self.assertIn("arguments", data["error"]["message"])

    def test_batch_request_returns_parse_error(self):
        """Negative: JSON-RPC batch request (array) is not supported and returns -32600."""
        batch = [
            {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {}},
            {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}},
        ]
        response = self.client.post(self.url, data=json.dumps(batch), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertNotIn("result", data)
        self.assertEqual(data["error"]["code"], -32600)

    def test_tools_call_invalid_params_returns_32602(self):
        """Negative: passing wrong argument types returns JSON-RPC -32602."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 31,
            "params": {"name": "hello", "arguments": {"message": 123, "unknown_param": "x"}},
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertNotIn("result", data)
        self.assertEqual(data["error"]["code"], -32602)

    # --- Authentication tests ---

    def test_hello_works_without_auth(self):
        """Positive: hello tool works without x-rh-identity header."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 10,
            "params": {"name": "hello", "arguments": {"message": "Hey!"}},
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = json.loads(response.json()["result"]["content"][0]["text"])
        self.assertEqual(tool_output["response"], "RBAC received your message: 'Hey!'")

    def test_list_principals_without_auth_returns_error(self):
        """Permission: list_principals without x-rh-identity returns auth error."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 11,
            "params": {"name": "list_principals", "arguments": {}},
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertNotIn("result", data)
        self.assertEqual(data["error"]["code"], -32000)

    # --- Edge cases ---

    def test_tools_call_with_default_arguments(self):
        """Edge case: tools/call with empty arguments uses defaults."""
        with patch(
            "management.principal.proxy.PrincipalProxy.request_principals",
            return_value={"status_code": 200, "data": [], "userCount": 0},
        ) as mock_request:
            body = {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 11,
                "params": {
                    "name": "list_principals",
                    "arguments": {},
                },
            }
            response = self.client.post(
                self.url, data=json.dumps(body), content_type="application/json", **self.headers
            )

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            mock_request.assert_called_once_with(
                org_id=self.customer_data["org_id"],
                limit=10,
                offset=0,
                options={
                    "limit": 10,
                    "offset": 0,
                    "sort_order": "asc",
                    "status": "enabled",
                    "username_only": "false",
                    "principal_type": "user",
                    "admin_only": "false",
                },
            )

    def test_tools_call_with_invalid_sort_order_returns_validation_error(self):
        """Edge case: Invalid sort_order returns validation error from PrincipalView."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 12,
            "params": {
                "name": "list_principals",
                "arguments": {"sort_order": "invalid"},
            },
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertNotIn("error", data)
        result = data["result"]
        self.assertFalse(result["isError"])
        tool_output = json.loads(result["content"][0]["text"])
        self.assertIn("errors", tool_output)
        self.assertIn("invalid", tool_output["errors"][0]["detail"])

    def test_tools_call_with_string_id(self):
        """Edge case: JSON-RPC request with string id works."""
        with patch(
            "management.principal.proxy.PrincipalProxy.request_principals",
            return_value={"status_code": 200, "data": [], "userCount": 0},
        ):
            body = {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": "abc-123",
                "params": {
                    "name": "list_principals",
                    "arguments": {},
                },
            }
            response = self.client.post(
                self.url, data=json.dumps(body), content_type="application/json", **self.headers
            )

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            data = response.json()
            self.assertEqual(data["id"], "abc-123")

    # --- New read-only tools tests ---

    def test_tools_list_includes_non_v2_tools(self):
        """Positive: tools/list includes all non-V2-only tools when V2 is disabled."""
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_names = [t["name"] for t in response.json()["result"]["tools"]]
        expected_tools = [
            "get_status",
            "list_access",
            "list_permissions",
            "list_permission_options",
            "list_audit_logs",
            "get_rbac_recent_changes",
            "search_roles",
            "get_role",
            "list_role_access",
            "list_groups",
            "get_group",
            "list_group_principals",
            "list_group_roles",
            "list_cross_account_requests",
            "get_cross_account_request",
            "investigate_tam_access",
            "list_workspaces",
            "get_workspace",
            "check_user_permission",
        ]
        for tool in expected_tools:
            self.assertIn(tool, tool_names, f"Tool '{tool}' missing from tools/list")

    def test_tools_list_excludes_v2_only_tools_when_v2_disabled(self):
        """Negative: V2-only tools are hidden when V2_APIS_ENABLED=False."""
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_names = [t["name"] for t in response.json()["result"]["tools"]]
        v2_only_tools = [name for name, cfg in _TOOL_CONFIG.items() if cfg.api_version == ApiVersion.V2]
        for tool in v2_only_tools:
            self.assertNotIn(tool, tool_names, f"V2-only tool '{tool}' should be hidden")

    # --- get_status ---

    def test_get_status_returns_server_info(self):
        """Positive: get_status returns server status with auth."""
        response = self._call_tool("get_status")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("api_version", tool_output)
        self.assertIn("commit", tool_output)

    # --- list_permissions ---

    def test_list_permissions_success(self):
        """Positive: list_permissions returns permission data."""
        Permission.objects.create(
            application="rbac",
            resource_type="roles",
            verb="read",
            permission="rbac:roles:read",
            tenant=self.tenant,
        )
        response = self._call_tool("list_permissions")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("meta", tool_output)
        self.assertIn("data", tool_output)

    def test_list_permissions_filters_by_application(self):
        """Positive: list_permissions filters results by application argument."""
        Permission.objects.create(
            application="rbac",
            resource_type="roles",
            verb="read",
            permission="rbac:roles:read",
            tenant=self.tenant,
        )
        Permission.objects.create(
            application="cost-management",
            resource_type="reports",
            verb="read",
            permission="cost-management:reports:read",
            tenant=self.tenant,
        )
        response = self._call_tool("list_permissions", {"application": "rbac"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        permissions = tool_output["data"]
        self.assertTrue(len(permissions) >= 1)
        for perm in permissions:
            self.assertTrue(perm["permission"].startswith("rbac:"))

    def test_list_permissions_without_auth_returns_error(self):
        """Permission: list_permissions without auth returns auth error."""
        response = self._call_tool("list_permissions", use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    # --- list_audit_logs ---

    def test_list_audit_logs_success(self):
        """Positive: list_audit_logs returns audit log data."""
        response = self._call_tool("list_audit_logs")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("meta", tool_output)
        self.assertIn("data", tool_output)

    def test_list_audit_logs_without_auth_returns_error(self):
        """Permission: list_audit_logs without auth returns auth error."""
        response = self._call_tool("list_audit_logs", use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_list_audit_logs_filter_by_principal_username(self):
        """Positive: list_audit_logs filters by principal_username."""
        AuditLog.objects.create(
            principal_username="jdoe",
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="role added to group",
            tenant=self.tenant,
        )
        AuditLog.objects.create(
            principal_username="other_user",
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="other action",
            tenant=self.tenant,
        )

        response = self._call_tool("list_audit_logs", {"principal_username": "jdoe"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["data"]), 1)
        self.assertEqual(tool_output["data"][0]["principal_username"], "jdoe")

    def test_list_audit_logs_filter_by_resource_type(self):
        """Positive: list_audit_logs filters by resource_type."""
        AuditLog.objects.create(
            principal_username="user1",
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="group action",
            tenant=self.tenant,
        )
        AuditLog.objects.create(
            principal_username="user1",
            resource_type=AuditLog.ROLE,
            action=AuditLog.CREATE,
            description="role action",
            tenant=self.tenant,
        )

        response = self._call_tool("list_audit_logs", {"resource_type": "group"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["data"]), 1)
        self.assertEqual(tool_output["data"][0]["resource_type"], "group")

    def test_list_audit_logs_filter_by_action(self):
        """Positive: list_audit_logs filters by action."""
        AuditLog.objects.create(
            principal_username="user1",
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="add action",
            tenant=self.tenant,
        )
        AuditLog.objects.create(
            principal_username="user1",
            resource_type=AuditLog.GROUP,
            action=AuditLog.DELETE,
            description="delete action",
            tenant=self.tenant,
        )

        response = self._call_tool("list_audit_logs", {"action": "add"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["data"]), 1)
        self.assertEqual(tool_output["data"][0]["action"], "add")

    @patch(
        "management.mcp_views.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": [{"is_org_admin": False}]},
    )
    def test_list_audit_logs_include_authorization(self, mock_proxy):
        """Positive: list_audit_logs with include_authorization returns role and permission."""
        # Create audit log entry
        AuditLog.objects.create(
            principal_username=self.principal.username,
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="Vulnerability administrator role added to group Contractors",
            tenant=self.tenant,
        )

        # Set up authorization chain: principal -> group -> policy -> role -> access -> permission
        role = Role.objects.create(name="User Access administrator", tenant=self.tenant)
        perm = Permission.objects.create(
            application="rbac",
            resource_type="group",
            verb="write",
            permission="rbac:group:write",
            tenant=self.tenant,
        )
        Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        group = Group.objects.create(name="Access Governance", tenant=self.tenant)
        group.principals.add(self.principal)
        policy = Policy.objects.create(name="auth_policy", group=group, tenant=self.tenant)
        policy.roles.add(role)

        response = self._call_tool("list_audit_logs", {"include_authorization": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["data"]), 1)
        entry = tool_output["data"][0]
        self.assertIn("authorized_by", entry)
        self.assertEqual(entry["authorized_by"]["role"], "User Access administrator")
        self.assertEqual(entry["authorized_by"]["via_group"], "Access Governance")
        self.assertEqual(entry["authorized_by"]["permission"], "rbac:group:write")

    @patch(
        "management.mcp_views.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": []},
    )
    def test_list_audit_logs_include_authorization_user_not_found(self, mock_proxy):
        """Positive: list_audit_logs handles deleted/unknown users."""
        AuditLog.objects.create(
            principal_username="deleted_user",
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="action by deleted user",
            tenant=self.tenant,
        )

        response = self._call_tool("list_audit_logs", {"include_authorization": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        entry = tool_output["data"][0]
        self.assertIsNone(entry["authorized_by"])
        self.assertIn("note", entry)
        self.assertIn("deleted_user", entry["note"])

    @patch(
        "management.mcp_views.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": [{"is_org_admin": True}]},
    )
    def test_list_audit_logs_include_authorization_org_admin(self, mock_proxy):
        """Positive: list_audit_logs shows org admin bypasses RBAC checks."""
        AuditLog.objects.create(
            principal_username="org_admin_user",
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="action by org admin",
            tenant=self.tenant,
        )

        response = self._call_tool("list_audit_logs", {"include_authorization": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        entry = tool_output["data"][0]
        self.assertIsNotNone(entry["authorized_by"])
        self.assertEqual(entry["authorized_by"]["role"], "Org Admin")
        self.assertIn("bypasses all RBAC", entry["authorized_by"]["permission"])

    def test_list_audit_logs_invalid_order_by(self):
        """Negative: list_audit_logs with invalid order_by returns error."""
        response = self._call_tool("list_audit_logs", {"order_by": "invalid_field"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("Invalid order_by", tool_output["error"])

    def test_list_audit_logs_empty_page_returns_total_count(self):
        """Positive: list_audit_logs returns total count even when offset is past end."""
        AuditLog.objects.create(
            principal_username="user1",
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="test entry",
            tenant=self.tenant,
        )

        response = self._call_tool("list_audit_logs", {"offset": 1000, "limit": 10})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["data"]), 0)
        self.assertEqual(tool_output["meta"]["count"], 1)

    def test_list_audit_logs_filter_by_group_name(self):
        """Positive: list_audit_logs filters by group_name in description."""
        AuditLog.objects.create(
            principal_username="user1",
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="role test_role added to group: target_group_alpha",
            tenant=self.tenant,
        )
        AuditLog.objects.create(
            principal_username="user2",
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="role other_role added to group: other_group_beta",
            tenant=self.tenant,
        )

        response = self._call_tool("list_audit_logs", {"group_name": "target_group_alpha"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["meta"]["count"], 1)
        self.assertEqual(len(tool_output["data"]), 1)
        self.assertIn("target_group_alpha", tool_output["data"][0]["description"])

    def test_list_audit_logs_filter_by_role_name(self):
        """Positive: list_audit_logs filters by role_name in description."""
        AuditLog.objects.create(
            principal_username="user1",
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="role target_role_alpha added to group: some_group",
            tenant=self.tenant,
        )
        AuditLog.objects.create(
            principal_username="user2",
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="role other_role_beta added to group: some_group",
            tenant=self.tenant,
        )

        response = self._call_tool("list_audit_logs", {"role_name": "target_role_alpha"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["meta"]["count"], 1)
        self.assertEqual(len(tool_output["data"]), 1)
        self.assertIn("target_role_alpha", tool_output["data"][0]["description"])

    # --- get_rbac_recent_changes ---

    def test_get_rbac_recent_changes_success(self):
        """Positive: get_rbac_recent_changes returns summary of recent changes."""
        response = self._call_tool("get_rbac_recent_changes")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("summary", tool_output)
        self.assertIn("by_resource_type", tool_output)
        self.assertIn("by_action", tool_output)
        self.assertIn("by_actor", tool_output)
        self.assertIn("recent_changes", tool_output)

    def test_get_rbac_recent_changes_without_auth_returns_error(self):
        """Permission: get_rbac_recent_changes without auth returns auth error."""
        response = self._call_tool("get_rbac_recent_changes", use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_get_rbac_recent_changes_with_audit_data(self):
        """Positive: get_rbac_recent_changes groups and summarizes audit data."""
        AuditLog.objects.create(
            principal_username="actor1",
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="user added to group",
            tenant=self.tenant,
        )
        AuditLog.objects.create(
            principal_username="actor1",
            resource_type=AuditLog.ROLE,
            action=AuditLog.CREATE,
            description="Created role: test_role",
            tenant=self.tenant,
        )
        AuditLog.objects.create(
            principal_username="actor2",
            resource_type=AuditLog.GROUP,
            action=AuditLog.DELETE,
            description="Deleted group: old_group",
            tenant=self.tenant,
        )

        response = self._call_tool("get_rbac_recent_changes", {"days": 7})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["summary"]["total_changes"], 3)
        self.assertEqual(tool_output["summary"]["unique_actors"], 2)
        self.assertIn("group", tool_output["by_resource_type"])
        self.assertIn("role", tool_output["by_resource_type"])
        self.assertIn("add", tool_output["by_action"])
        self.assertIn("create", tool_output["by_action"])
        self.assertIn("delete", tool_output["by_action"])

    def test_get_rbac_recent_changes_days_parameter(self):
        """Positive: get_rbac_recent_changes respects days parameter."""
        old_entry = AuditLog.objects.create(
            principal_username="old_actor",
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="old action",
            tenant=self.tenant,
        )
        old_entry.created = timezone.now() - __import__("datetime").timedelta(days=10)
        old_entry.save()

        AuditLog.objects.create(
            principal_username="recent_actor",
            resource_type=AuditLog.GROUP,
            action=AuditLog.ADD,
            description="recent action",
            tenant=self.tenant,
        )

        response = self._call_tool("get_rbac_recent_changes", {"days": 7})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["summary"]["total_changes"], 1)
        self.assertIn("recent_actor", tool_output["by_actor"])
        self.assertNotIn("old_actor", tool_output["by_actor"])

    def test_get_rbac_recent_changes_empty_result(self):
        """Positive: get_rbac_recent_changes returns clean empty state when no changes."""
        response = self._call_tool("get_rbac_recent_changes", {"days": 1})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["summary"]["total_changes"], 0)
        self.assertEqual(tool_output["by_resource_type"], {})
        self.assertEqual(tool_output["by_action"], {})

    def test_get_rbac_recent_changes_clamps_days(self):
        """Positive: get_rbac_recent_changes clamps days to valid range (1-30)."""
        response = self._call_tool("get_rbac_recent_changes", {"days": 100})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["summary"]["days_reviewed"], 30)

        response = self._call_tool("get_rbac_recent_changes", {"days": 0})
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["summary"]["days_reviewed"], 1)

    # --- investigate_group_changes ---

    def test_investigate_group_changes_success(self):
        """Positive: investigate_group_changes returns group info and audit entries."""
        group = Group.objects.create(name="Contractors", tenant=self.tenant)
        AuditLog.objects.create(
            principal_username="jdoe",
            resource_type=AuditLog.GROUP,
            resource_uuid=group.uuid,
            action=AuditLog.ADD,
            description="Vulnerability administrator role added to group Contractors",
            tenant=self.tenant,
        )

        response = self._call_tool("investigate_group_changes", {"group_name": "Contractors"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertIn("group", tool_output)
        self.assertEqual(tool_output["group"]["name"], "Contractors")
        self.assertIn("audit_entries", tool_output)
        self.assertEqual(len(tool_output["audit_entries"]), 1)
        self.assertEqual(tool_output["audit_entries"][0]["actor"], "jdoe")

    def test_investigate_group_changes_without_auth_returns_error(self):
        """Permission: investigate_group_changes without auth returns auth error."""
        response = self._call_tool("investigate_group_changes", {"group_name": "Test"}, use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_investigate_group_changes_group_not_found(self):
        """Negative: investigate_group_changes returns error when group not found."""
        response = self._call_tool("investigate_group_changes", {"group_name": "NonExistent"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("not found", tool_output["error"])

    def test_investigate_group_changes_with_suggestions(self):
        """Positive: investigate_group_changes suggests similar groups on partial match failure."""
        Group.objects.create(name="Contractors-East", tenant=self.tenant)
        Group.objects.create(name="Contractors-West", tenant=self.tenant)

        response = self._call_tool("investigate_group_changes", {"group_name": "Contractors-North"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("did_you_mean", tool_output)
        self.assertEqual(len(tool_output["did_you_mean"]), 2)

    def test_investigate_group_changes_filter_by_role_name(self):
        """Positive: investigate_group_changes filters by role_name."""
        group = Group.objects.create(name="TestGroup", tenant=self.tenant)
        AuditLog.objects.create(
            principal_username="user1",
            resource_type=AuditLog.GROUP,
            resource_uuid=group.uuid,
            action=AuditLog.ADD,
            description="Vulnerability administrator role added",
            tenant=self.tenant,
        )
        AuditLog.objects.create(
            principal_username="user2",
            resource_type=AuditLog.GROUP,
            resource_uuid=group.uuid,
            action=AuditLog.ADD,
            description="Cost Management reader role added",
            tenant=self.tenant,
        )

        response = self._call_tool(
            "investigate_group_changes",
            {"group_name": "TestGroup", "role_name": "Vulnerability"},
        )

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["audit_entries"]), 1)
        self.assertIn("Vulnerability", tool_output["audit_entries"][0]["description"])

    def test_investigate_group_changes_filter_by_action(self):
        """Positive: investigate_group_changes filters by action."""
        group = Group.objects.create(name="TestGroup", tenant=self.tenant)
        AuditLog.objects.create(
            principal_username="user1",
            resource_type=AuditLog.GROUP,
            resource_uuid=group.uuid,
            action=AuditLog.ADD,
            description="role added",
            tenant=self.tenant,
        )
        AuditLog.objects.create(
            principal_username="user2",
            resource_type=AuditLog.GROUP,
            resource_uuid=group.uuid,
            action=AuditLog.DELETE,
            description="role deleted",
            tenant=self.tenant,
        )

        response = self._call_tool(
            "investigate_group_changes",
            {"group_name": "TestGroup", "action": "add"},
        )

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["audit_entries"]), 1)
        self.assertEqual(tool_output["audit_entries"][0]["action"], "add")

    @patch(
        "management.mcp_views.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": [{"is_org_admin": False}]},
    )
    def test_investigate_group_changes_with_authorization(self, mock_proxy):
        """Positive: investigate_group_changes includes authorization context."""
        group = Group.objects.create(name="Contractors", tenant=self.tenant)
        AuditLog.objects.create(
            principal_username=self.principal.username,
            resource_type=AuditLog.GROUP,
            resource_uuid=group.uuid,
            action=AuditLog.ADD,
            description="Vulnerability administrator role added",
            tenant=self.tenant,
        )

        # Set up authorization chain
        role = Role.objects.create(name="User Access administrator", tenant=self.tenant)
        perm = Permission.objects.create(
            application="rbac",
            resource_type="group",
            verb="write",
            permission="rbac:group:write",
            tenant=self.tenant,
        )
        Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        auth_group = Group.objects.create(name="Access Governance", tenant=self.tenant)
        auth_group.principals.add(self.principal)
        policy = Policy.objects.create(name="auth_policy", group=auth_group, tenant=self.tenant)
        policy.roles.add(role)

        response = self._call_tool(
            "investigate_group_changes",
            {"group_name": "Contractors", "include_authorization": True},
        )

        tool_output = self._get_tool_output(response)
        entry = tool_output["audit_entries"][0]
        self.assertIn("authorized_by", entry)
        self.assertEqual(entry["authorized_by"]["role"], "User Access administrator")
        self.assertEqual(entry["authorized_by"]["via_group"], "Access Governance")
        self.assertEqual(entry["authorized_by"]["permission"], "rbac:group:write")

    def test_investigate_group_changes_shows_current_roles(self):
        """Positive: investigate_group_changes includes current roles on the group."""
        group = Group.objects.create(name="TestGroup", tenant=self.tenant)
        role = Role.objects.create(name="TestRole", display_name="Test Role Display", tenant=self.tenant)
        policy = Policy.objects.create(name="test_policy", group=group, tenant=self.tenant)
        policy.roles.add(role)

        response = self._call_tool("investigate_group_changes", {"group_name": "TestGroup"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["group"]["current_role_count"], 1)
        self.assertEqual(len(tool_output["group"]["current_roles"]), 1)
        self.assertEqual(tool_output["group"]["current_roles"][0]["name"], "TestRole")

    def test_investigate_group_changes_role_currently_assigned(self):
        """Positive: investigate_group_changes indicates if queried role is currently assigned."""
        group = Group.objects.create(name="Contractors", tenant=self.tenant)
        role = Role.objects.create(
            name="Vulnerability administrator", display_name="Vulnerability administrator", tenant=self.tenant
        )
        policy = Policy.objects.create(name="test_policy", group=group, tenant=self.tenant)
        policy.roles.add(role)

        AuditLog.objects.create(
            principal_username="jdoe",
            resource_type=AuditLog.GROUP,
            resource_uuid=group.uuid,
            action=AuditLog.ADD,
            description="Vulnerability administrator role added",
            tenant=self.tenant,
        )

        response = self._call_tool(
            "investigate_group_changes",
            {"group_name": "Contractors", "role_name": "Vulnerability"},
        )

        tool_output = self._get_tool_output(response)
        self.assertTrue(tool_output["role_currently_assigned"])
        self.assertEqual(len(tool_output["matching_current_roles"]), 1)

    def test_investigate_group_changes_summary_statistics(self):
        """Positive: investigate_group_changes returns summary statistics."""
        group = Group.objects.create(name="TestGroup", tenant=self.tenant)
        AuditLog.objects.create(
            principal_username="user1",
            resource_type=AuditLog.GROUP,
            resource_uuid=group.uuid,
            action=AuditLog.ADD,
            description="action1",
            tenant=self.tenant,
        )
        AuditLog.objects.create(
            principal_username="user1",
            resource_type=AuditLog.GROUP,
            resource_uuid=group.uuid,
            action=AuditLog.ADD,
            description="action2",
            tenant=self.tenant,
        )
        AuditLog.objects.create(
            principal_username="user2",
            resource_type=AuditLog.GROUP,
            resource_uuid=group.uuid,
            action=AuditLog.DELETE,
            description="action3",
            tenant=self.tenant,
        )

        response = self._call_tool("investigate_group_changes", {"group_name": "TestGroup"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["summary"]["total_changes_found"], 3)
        self.assertEqual(tool_output["summary"]["unique_actors"], 2)
        self.assertIn("user1", tool_output["summary"]["actors"])
        self.assertIn("user2", tool_output["summary"]["actors"])
        self.assertEqual(tool_output["summary"]["by_action"]["add"], 2)
        self.assertEqual(tool_output["summary"]["by_action"]["delete"], 1)

    def test_investigate_group_changes_case_insensitive_group_name(self):
        """Positive: investigate_group_changes finds group with case-insensitive name."""
        Group.objects.create(name="Contractors", tenant=self.tenant)

        response = self._call_tool("investigate_group_changes", {"group_name": "contractors"})

        tool_output = self._get_tool_output(response)
        self.assertIn("group", tool_output)
        self.assertEqual(tool_output["group"]["name"], "Contractors")

    def test_investigate_group_changes_no_audit_entries(self):
        """Positive: investigate_group_changes returns empty audit_entries when no changes found."""
        Group.objects.create(name="NewGroup", tenant=self.tenant)

        response = self._call_tool("investigate_group_changes", {"group_name": "NewGroup"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["audit_entries"]), 0)
        self.assertEqual(tool_output["summary"]["total_changes_found"], 0)
        self.assertIn("message", tool_output["summary"])

    # --- list_groups / get_group / list_group_principals ---

    def test_list_groups_success(self):
        """Positive: list_groups returns group data."""
        Group.objects.create(name="test_group", tenant=self.tenant)
        response = self._call_tool("list_groups")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("meta", tool_output)
        self.assertIn("data", tool_output)

    def test_list_groups_without_auth_returns_error(self):
        """Permission: list_groups without auth returns auth error."""
        response = self._call_tool("list_groups", use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_get_group_success(self):
        """Positive: get_group returns group detail."""
        group = Group.objects.create(name="detail_group", tenant=self.tenant)
        response = self._call_tool("get_group", {"group_uuid": str(group.uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["name"], "detail_group")

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [{"username": "test_user"}],
        },
    )
    def test_list_group_principals_success(self, mock_request):
        """Positive: list_group_principals returns principals in group."""
        group = Group.objects.create(name="principals_group", tenant=self.tenant)
        group.principals.add(self.principal)
        response = self._call_tool("list_group_principals", {"group_uuid": str(group.uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("data", tool_output)

    # --- list_cross_account_requests / get_cross_account_request ---

    def test_list_cross_account_requests_success(self):
        """Positive: list_cross_account_requests returns request data."""
        response = self._call_tool("list_cross_account_requests")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("meta", tool_output)
        self.assertIn("data", tool_output)

    def test_list_cross_account_requests_without_auth_returns_error(self):
        """Permission: list_cross_account_requests without auth returns auth error."""
        response = self._call_tool("list_cross_account_requests", use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    # --- list_access ---

    def test_list_access_success(self):
        """Positive: list_access returns access data for the authenticated principal."""
        role = Role.objects.create(name="access_role", tenant=self.tenant)
        perm = Permission.objects.create(
            application="rbac",
            resource_type="roles",
            verb="read",
            permission="rbac:roles:read",
            tenant=self.tenant,
        )
        Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        group = Group.objects.create(name="access_group", tenant=self.tenant)
        group.principals.add(self.principal)
        policy = Policy.objects.create(name="access_policy", group=group, tenant=self.tenant)
        policy.roles.add(role)

        response = self._call_tool("list_access", {"application": "rbac"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("data", tool_output)

    def test_list_access_without_auth_returns_error(self):
        """Permission: list_access without auth returns auth error."""
        response = self._call_tool("list_access", {"application": "rbac"}, use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    # --- list_permission_options ---

    def test_list_permission_options_success(self):
        """Positive: list_permission_options returns distinct field values."""
        Permission.objects.create(
            application="rbac",
            resource_type="roles",
            verb="read",
            permission="rbac:roles:read",
            tenant=self.tenant,
        )
        Permission.objects.create(
            application="cost-management",
            resource_type="reports",
            verb="read",
            permission="cost-management:reports:read",
            tenant=self.tenant,
        )
        response = self._call_tool("list_permission_options", {"field": "application"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("data", tool_output)
        self.assertIn("meta", tool_output)

    def test_list_permission_options_without_auth_returns_error(self):
        """Permission: list_permission_options without auth returns auth error."""
        response = self._call_tool("list_permission_options", {"field": "application"}, use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    # --- list_group_roles ---

    def test_list_group_roles_success(self):
        """Positive: list_group_roles returns roles for a group."""
        group = Group.objects.create(name="roles_group", tenant=self.tenant)
        role = Role.objects.create(name="test_role", tenant=self.tenant)
        policy = Policy.objects.create(name="test_policy", group=group, tenant=self.tenant)
        policy.roles.add(role)

        response = self._call_tool("list_group_roles", {"group_uuid": str(group.uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("data", tool_output)

    def test_list_group_roles_without_auth_returns_error(self):
        """Permission: list_group_roles without auth returns auth error."""
        response = self._call_tool(
            "list_group_roles",
            {"group_uuid": "00000000-0000-0000-0000-000000000000"},
            use_auth=False,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_list_group_roles_by_name(self):
        """Positive: list_group_roles accepts group_name instead of group_uuid."""
        group = Group.objects.create(name="group_for_roles_test", tenant=self.tenant)
        role = Role.objects.create(name="role_alpha", tenant=self.tenant)
        policy = Policy.objects.create(name="policy_alpha", group=group, tenant=self.tenant)
        policy.roles.add(role)

        response = self._call_tool("list_group_roles", {"group_name": "group_for_roles_test"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("data", tool_output)
        self.assertEqual(tool_output["meta"]["count"], 1)
        self.assertEqual(tool_output["data"][0]["name"], "role_alpha")

    def test_list_group_roles_by_name_case_insensitive(self):
        """Positive: list_group_roles group_name lookup is case-insensitive."""
        group = Group.objects.create(name="Group_With_Mixed_Case", tenant=self.tenant)
        role = Role.objects.create(name="role_beta", tenant=self.tenant)
        policy = Policy.objects.create(name="policy_beta", group=group, tenant=self.tenant)
        policy.roles.add(role)

        response = self._call_tool("list_group_roles", {"group_name": "group_with_mixed_case"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["meta"]["count"], 1)
        self.assertEqual(tool_output["data"][0]["name"], "role_beta")

    def test_list_group_roles_missing_both_params_returns_error(self):
        """Negative: list_group_roles without group_uuid or group_name returns error."""
        response = self._call_tool("list_group_roles", {})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("Either group_uuid or group_name is required", tool_output["error"])

    def test_list_group_roles_group_not_found_returns_error(self):
        """Negative: list_group_roles with non-existent group_name returns error."""
        response = self._call_tool("list_group_roles", {"group_name": "nonexistent_group_xyz"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("not found", tool_output["error"])

    # --- list_role_access ---

    def test_list_role_access_success(self):
        """Positive: list_role_access returns access for a role."""
        role = Role.objects.create(name="access_detail_role", tenant=self.tenant)
        perm = Permission.objects.create(
            application="rbac",
            resource_type="principals",
            verb="read",
            permission="rbac:principals:read",
            tenant=self.tenant,
        )
        Access.objects.create(permission=perm, role=role, tenant=self.tenant)

        response = self._call_tool("list_role_access", {"role_uuid": str(role.uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("data", tool_output)
        self.assertTrue(len(tool_output["data"]) >= 1)

    def test_list_role_access_without_auth_returns_error(self):
        """Permission: list_role_access without auth returns auth error."""
        response = self._call_tool(
            "list_role_access",
            {"role_uuid": "00000000-0000-0000-0000-000000000000"},
            use_auth=False,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)


@override_settings(V2_APIS_ENABLED=True)
class MCPViewV2ToolsTests(MCPToolTestMixin, IdentityRequest):
    """Test the MCP V2 tools that require V2_APIS_ENABLED=True."""

    def setUp(self):
        """Set up the MCP V2 tool tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )

    def tearDown(self):
        """Tear down MCP V2 tool tests."""
        Principal.objects.all().delete()
        super().tearDown()

    # --- V2 gating: tools/list includes V2-only tools ---

    def test_tools_list_includes_v2_only_tools_when_v2_enabled(self):
        """Positive: V2-only tools are visible when V2_APIS_ENABLED=True."""
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_names = [t["name"] for t in response.json()["result"]["tools"]]
        self.assertIn("list_role_bindings", tool_names)
        self.assertIn("list_role_bindings_by_subject", tool_names)

    # --- list_workspaces / get_workspace ---

    def test_list_workspaces_success(self):
        """Positive: list_workspaces returns workspace data."""
        response = self._call_tool("list_workspaces")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("meta", tool_output)
        self.assertIn("data", tool_output)

    def test_list_workspaces_without_auth_returns_error(self):
        """Permission: list_workspaces without auth returns auth error."""
        response = self._call_tool("list_workspaces", use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    # --- list_role_bindings ---

    def test_list_role_bindings_success(self):
        """Positive: list_role_bindings returns role binding data."""
        response = self._call_tool("list_role_bindings")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("meta", tool_output)
        self.assertIn("data", tool_output)

    def test_list_role_bindings_without_auth_returns_error(self):
        """Permission: list_role_bindings without auth returns auth error."""
        response = self._call_tool("list_role_bindings", use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    # --- list_role_bindings_by_subject ---

    def test_list_role_bindings_by_subject_without_auth_returns_error(self):
        """Permission: list_role_bindings_by_subject without auth returns auth error."""
        response = self._call_tool(
            "list_role_bindings_by_subject",
            {"resource_id": "test-id", "resource_type": "workspace"},
            use_auth=False,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)


class PermissionMatchesTests(IdentityRequest):
    """Unit tests for the _permission_matches helper function."""

    def test_exact_match(self):
        """Positive: exact permission match returns True."""
        self.assertTrue(_permission_matches("cost-management:cost_model:write", "cost-management:cost_model:write"))

    def test_wildcard_all(self):
        """Positive: full wildcard matches any permission in the same app."""
        self.assertTrue(_permission_matches("cost-management:*:*", "cost-management:cost_model:write"))

    def test_wildcard_verb(self):
        """Positive: verb wildcard matches any verb."""
        self.assertTrue(_permission_matches("cost-management:cost_model:*", "cost-management:cost_model:write"))

    def test_mismatch_verb(self):
        """Negative: different verb does not match."""
        self.assertFalse(_permission_matches("cost-management:cost_model:read", "cost-management:cost_model:write"))

    def test_mismatch_app(self):
        """Negative: different application does not match."""
        self.assertFalse(_permission_matches("inventory:hosts:read", "cost-management:hosts:read"))

    def test_malformed_granted(self):
        """Negative: malformed granted permission returns False."""
        self.assertFalse(_permission_matches("invalid-format", "cost-management:cost_model:write"))

    def test_malformed_requested(self):
        """Negative: malformed requested permission returns False."""
        self.assertFalse(_permission_matches("cost-management:cost_model:write", "invalid"))


@override_settings(BYPASS_BOP_VERIFICATION=True)
class MCPCheckUserPermissionTests(MCPToolTestMixin, IdentityRequest):
    """Tests for the check_user_permission MCP tool."""

    def setUp(self):
        """Set up check_user_permission tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.test_username = self.user_data["username"]
        self.principal = Principal.objects.create(username=self.test_username, tenant=self.tenant)

        role = Role.objects.create(name="test_role", tenant=self.tenant)
        perm = Permission.objects.create(
            application="rbac",
            resource_type="roles",
            verb="read",
            permission="rbac:roles:read",
            tenant=self.tenant,
        )
        Access.objects.create(permission=perm, role=role, tenant=self.tenant)
        group = Group.objects.create(name="test_group", tenant=self.tenant)
        group.principals.add(self.principal)
        policy = Policy.objects.create(name="test_policy", group=group, tenant=self.tenant)
        policy.roles.add(role)

    def tearDown(self):
        """Tear down check_user_permission tests."""
        Policy.objects.all().delete()
        Access.objects.all().delete()
        Role.objects.all().delete()
        Permission.objects.all().delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    def test_invalid_permission_format(self):
        """Negative: invalid permission format returns error."""
        response = self._call_tool("check_user_permission", {"username": self.test_username, "permission": "invalid"})

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("application:resource_type:verb", tool_output["error"])

    def test_permission_allowed(self):
        """Positive: check_user_permission returns allowed=True when user has permission."""
        response = self._call_tool(
            "check_user_permission", {"username": self.test_username, "permission": "rbac:roles:read"}
        )

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertTrue(tool_output["allowed"])
        self.assertEqual(tool_output["username"], self.test_username)
        self.assertEqual(tool_output["matched_permission"], "rbac:roles:read")

    def test_permission_denied(self):
        """Negative: check_user_permission returns allowed=False when user lacks permission."""
        response = self._call_tool(
            "check_user_permission", {"username": self.test_username, "permission": "rbac:roles:write"}
        )

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertFalse(tool_output["allowed"])
        self.assertIn("hint", tool_output)

    def test_without_auth_returns_error(self):
        """Permission: check_user_permission without auth returns auth error."""
        response = self._call_tool(
            "check_user_permission",
            {"username": self.test_username, "permission": "rbac:roles:read"},
            use_auth=False,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    @patch("management.mcp_views._list_principals_by_name")
    def test_check_user_permission_resolves_display_name(self, mock_by_name):
        """Positive: check_user_permission auto-resolves a display name to a username."""
        mock_by_name.return_value = json.dumps(
            {
                "data": [
                    {"username": self.test_username, "first_name": "Test", "last_name": "User"},
                ]
            }
        )
        response = self._call_tool("check_user_permission", {"username": "Test User", "permission": "rbac:roles:read"})

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertTrue(tool_output["allowed"])

    @patch("management.mcp_views._list_principals_by_name")
    def test_check_user_permission_multiple_name_matches(self, mock_by_name):
        """Negative: check_user_permission returns candidates when multiple users match."""
        mock_by_name.return_value = json.dumps(
            {
                "data": [
                    {"username": "user1", "first_name": "Test", "last_name": "Alpha"},
                    {"username": "user2", "first_name": "Test", "last_name": "Beta"},
                ]
            }
        )
        response = self._call_tool("check_user_permission", {"username": "Test", "permission": "rbac:roles:read"})

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("Multiple users match", tool_output["error"])
        self.assertEqual(len(tool_output["candidates"]), 2)


class MCPSearchRolesTests(MCPToolTestMixin, IdentityRequest):
    """Tests for the unified search_roles MCP tool (V1 path)."""

    def setUp(self):
        """Set up search_roles tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()

    def tearDown(self):
        """Tear down search_roles tests."""
        Role.objects.all().delete()
        super().tearDown()

    def test_search_roles_v1_success(self):
        """Positive: search_roles on V1 org returns role data with org_version=v1."""
        Role.objects.create(name="test_v1_role", tenant=self.tenant)
        response = self._call_tool("search_roles")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("meta", tool_output)
        self.assertIn("data", tool_output)
        self.assertEqual(tool_output["org_version"], "v1")

    def test_search_roles_v1_filter_by_name(self):
        """Positive: search_roles on V1 org filters by name."""
        Role.objects.create(name="Patch Reviewer", tenant=self.tenant)
        Role.objects.create(name="Other Role", tenant=self.tenant)
        response = self._call_tool("search_roles", {"name": "Patch Reviewer"})

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertTrue(tool_output["meta"]["count"] >= 1)
        for role in tool_output["data"]:
            self.assertIn("Patch Reviewer", role["name"])
        self.assertEqual(tool_output["org_version"], "v1")

    def test_search_roles_without_auth_returns_error(self):
        """Permission: search_roles without auth returns auth error."""
        response = self._call_tool("search_roles", use_auth=False)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    @override_settings(BYPASS_BOP_VERIFICATION=True)
    def test_search_roles_v1_filter_by_username(self):
        """Positive: search_roles on V1 org filters by username to show roles held by user."""
        principal = Principal.objects.create(username="role_holder", tenant=self.tenant)
        group = Group.objects.create(name="Test Group", tenant=self.tenant)
        group.principals.add(principal)
        role = Role.objects.create(name="User Role", tenant=self.tenant)
        policy = Policy.objects.create(name="Test Policy", group=group, tenant=self.tenant)
        policy.roles.add(role)

        Role.objects.create(name="Unassigned Role", tenant=self.tenant)

        response = self._call_tool("search_roles", {"username": "role_holder"})

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["org_version"], "v1")
        role_names = [r["name"] for r in tool_output["data"]]
        self.assertIn("User Role", role_names)
        self.assertNotIn("Unassigned Role", role_names)
        self.assertNotIn("ignored_filters", tool_output)


class MCPViewNonAdminTests(IdentityRequest):
    """Test the MCP endpoint for non-admin users via _private/_a2s/mcp/."""

    def setUp(self):
        """Set up the MCP view non-admin tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()

        non_admin_tenant_name = "acct1234"
        self.non_admin_tenant = Tenant.objects.create(
            tenant_name=non_admin_tenant_name, account_id="1234", org_id="4321"
        )

        self.user_data = {"username": "non_admin", "email": "non_admin@example.com"}
        self.customer = {"account_id": "1234", "org_id": "4321", "tenant_name": non_admin_tenant_name}
        self.request_context = self._create_request_context(self.customer, self.user_data, is_org_admin=False)

        request = self.request_context["request"]
        self.headers = request.META

        # Set up RBAC permission for non-admin to read principals
        self.non_admin_principal = Principal.objects.create(username="non_admin", tenant=self.non_admin_tenant)
        self.group = Group.objects.create(name="principal_readers", tenant=self.non_admin_tenant)
        self.group.principals.add(self.non_admin_principal)
        self.role = Role.objects.create(name="principal_reader_role", tenant=self.non_admin_tenant)
        self.permission = Permission.objects.create(
            application="rbac",
            resource_type="principals",
            verb="read",
            permission="rbac:principal:read",
            tenant=self.non_admin_tenant,
        )
        self.access = Access.objects.create(permission=self.permission, role=self.role, tenant=self.non_admin_tenant)
        self.policy = Policy.objects.create(
            name="principal_reader_policy", group=self.group, tenant=self.non_admin_tenant
        )
        self.policy.roles.add(self.role)

    def tearDown(self):
        """Tear down MCP view non-admin tests."""
        Policy.objects.all().delete()
        Access.objects.all().delete()
        Role.objects.all().delete()
        Permission.objects.all().delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    @patch(
        "management.principal.proxy.PrincipalProxy.request_principals",
        return_value={"status_code": 200, "data": [], "userCount": 0},
    )
    def test_non_admin_can_call_list_principals(self, mock_request):
        """Positive: Non-admin users with principal:read permission can call list_principals tool."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 1,
            "params": {
                "name": "list_principals",
                "arguments": {},
            },
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("result", data)
        result = data["result"]
        self.assertFalse(result["isError"])
        tool_output = json.loads(result["content"][0]["text"])
        self.assertIn("meta", tool_output)
        self.assertIn("data", tool_output)

    def test_non_admin_without_permission_gets_forbidden(self):
        """Permission: Non-admin without principal:read gets 403 from PrincipalView."""
        # Remove RBAC permission
        self.policy.roles.clear()

        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 2,
            "params": {
                "name": "list_principals",
                "arguments": {},
            },
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertNotIn("error", data)
        # PrincipalView returns 403, which is captured in the tool response
        result = data["result"]
        self.assertFalse(result["isError"])
        tool_output = json.loads(result["content"][0]["text"])
        self.assertIn("errors", tool_output)
        self.assertIsInstance(tool_output["errors"], list)
        self.assertGreater(len(tool_output["errors"]), 0)
        self.assertEqual(tool_output["errors"][0]["status"], "403")


@override_settings(BYPASS_BOP_VERIFICATION=True, V2_APIS_ENABLED=True)
class MCPCheckUserPermissionV2Tests(MCPToolTestMixin, IdentityRequest):
    """Tests for check_user_permission auto-detecting V2 orgs and using role bindings."""

    def setUp(self):
        """Set up V2 check_user_permission tests with tenant mapping and role bindings."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.test_username = self.user_data["username"]
        self.principal = Principal.objects.create(username=self.test_username, tenant=self.tenant)

        self.enterContext(
            patch(
                "management.permissions.role_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )

        # Activate V2 for this tenant
        TenantMapping.objects.create(tenant=self.tenant, v2_write_activated_at=timezone.now())

        # Create V2 role with a permission
        self.v2_perm = Permission.objects.create(
            application="vulnerability",
            resource_type="vulnerability",
            verb="read",
            permission="vulnerability:vulnerability:read",
            tenant=self.tenant,
        )
        self.v2_role = RoleV2.objects.create(name="Vuln Reader", tenant=self.tenant)
        self.v2_role.permissions.add(self.v2_perm)

        # Create role binding assigning the role directly to the principal
        self.binding = RoleBinding.objects.create(
            tenant=self.tenant,
            role=self.v2_role,
            resource_type="workspace",
            resource_id="root-workspace-id",
        )
        RoleBindingPrincipal.objects.create(binding=self.binding, principal=self.principal, source="direct")

    def tearDown(self):
        """Tear down V2 check_user_permission tests."""
        RoleBindingPrincipal.objects.all().delete()
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        RoleV2.objects.all().delete()
        Permission.objects.all().delete()
        TenantMapping.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    def test_v2_permission_allowed(self):
        """Positive: V2 org auto-detects and returns allowed=True via role bindings."""
        response = self._call_tool(
            "check_user_permission",
            {"username": self.test_username, "permission": "vulnerability:vulnerability:read"},
        )

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertTrue(tool_output["allowed"])
        self.assertEqual(tool_output["username"], self.test_username)
        self.assertEqual(tool_output["matched_permission"], "vulnerability:vulnerability:read")
        self.assertEqual(tool_output["role_name"], "Vuln Reader")
        self.assertEqual(tool_output["org_version"], "v2")

    def test_v2_permission_denied(self):
        """Negative: V2 org returns allowed=False when user lacks the permission."""
        response = self._call_tool(
            "check_user_permission",
            {"username": self.test_username, "permission": "vulnerability:vulnerability:write"},
        )

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertFalse(tool_output["allowed"])
        self.assertEqual(tool_output["org_version"], "v2")
        self.assertIn("hint", tool_output)

    def test_v2_user_not_found(self):
        """Negative: V2 org returns hint when user doesn't exist."""
        response = self._call_tool(
            "check_user_permission",
            {"username": "nonexistent_user", "permission": "vulnerability:vulnerability:read"},
        )

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertFalse(tool_output["allowed"])
        self.assertEqual(tool_output["org_version"], "v2")
        self.assertIn("not found", tool_output["hint"])

    def test_v2_permission_via_group(self):
        """Positive: V2 org resolves permissions inherited through group membership."""
        # Create a group and add the principal to it
        group = Group.objects.create(name="vuln_readers_group", tenant=self.tenant)
        group.principals.add(self.principal)

        # Create a separate role binding assigned to the group
        write_perm = Permission.objects.create(
            application="vulnerability",
            resource_type="vulnerability",
            verb="write",
            permission="vulnerability:vulnerability:write",
            tenant=self.tenant,
        )
        write_role = RoleV2.objects.create(name="Vuln Writer", tenant=self.tenant)
        write_role.permissions.add(write_perm)
        group_binding = RoleBinding.objects.create(
            tenant=self.tenant,
            role=write_role,
            resource_type="workspace",
            resource_id="root-workspace-id",
        )
        RoleBindingGroup.objects.create(binding=group_binding, group=group)

        response = self._call_tool(
            "check_user_permission",
            {"username": self.test_username, "permission": "vulnerability:vulnerability:write"},
        )

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertTrue(tool_output["allowed"])
        self.assertEqual(tool_output["role_name"], "Vuln Writer")
        self.assertEqual(tool_output["org_version"], "v2")

    def test_v2_wildcard_match(self):
        """Positive: V2 wildcard permission matching works."""
        wildcard_perm = Permission.objects.create(
            application="vulnerability",
            resource_type="vulnerability",
            verb="*",
            permission="vulnerability:vulnerability:*",
            tenant=self.tenant,
        )
        wildcard_role = RoleV2.objects.create(name="Vuln Wildcard", tenant=self.tenant)
        wildcard_role.permissions.add(wildcard_perm)
        wildcard_binding = RoleBinding.objects.create(
            tenant=self.tenant,
            role=wildcard_role,
            resource_type="workspace",
            resource_id="root-workspace-id",
        )
        RoleBindingPrincipal.objects.create(binding=wildcard_binding, principal=self.principal, source="direct")

        response = self._call_tool(
            "check_user_permission",
            {"username": self.test_username, "permission": "vulnerability:vulnerability:write"},
        )

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertTrue(tool_output["allowed"])
        self.assertEqual(tool_output["matched_permission"], "vulnerability:vulnerability:*")
        self.assertEqual(tool_output["org_version"], "v2")


@override_settings(BYPASS_BOP_VERIFICATION=True, V2_APIS_ENABLED=True)
class MCPUnifiedSearchRolesV2Tests(MCPToolTestMixin, IdentityRequest):
    """Tests for the unified search_roles MCP tool routing to V2."""

    def setUp(self):
        """Set up V2 search_roles tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )
        TenantMapping.objects.create(tenant=self.tenant, v2_write_activated_at=timezone.now())

    def tearDown(self):
        """Tear down V2 search_roles tests."""
        RoleV2.objects.all().delete()
        TenantMapping.objects.all().delete()
        super().tearDown()

    def test_search_roles_v2_success(self):
        """Positive: search_roles on V2 org returns role data with org_version=v2."""
        RoleV2.objects.create(name="V2 Custom Role", tenant=self.tenant, type=RoleV2.Types.CUSTOM)
        response = self._call_tool("search_roles")

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertIn("meta", tool_output)
        self.assertIn("data", tool_output)
        self.assertEqual(tool_output["org_version"], "v2")

    def test_search_roles_v2_filter_by_name(self):
        """Positive: search_roles on V2 org filters by name."""
        RoleV2.objects.create(name="Cost Reader", tenant=self.tenant, type=RoleV2.Types.CUSTOM)
        RoleV2.objects.create(name="Other V2 Role", tenant=self.tenant, type=RoleV2.Types.CUSTOM)
        response = self._call_tool("search_roles", {"name": "Cost Reader"})

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["org_version"], "v2")
        role_names = [r["name"] for r in tool_output["data"]]
        self.assertIn("Cost Reader", role_names)

    def test_search_roles_v2_filter_by_permission(self):
        """Positive: search_roles on V2 org filters by permission."""
        perm = Permission.objects.create(
            application="inventory",
            resource_type="hosts",
            verb="read",
            permission="inventory:hosts:read",
            tenant=self.tenant,
        )
        matching_role = RoleV2.objects.create(name="Host Reader", tenant=self.tenant, type=RoleV2.Types.CUSTOM)
        matching_role.permissions.add(perm)

        RoleV2.objects.create(name="Empty Role", tenant=self.tenant, type=RoleV2.Types.CUSTOM)

        response = self._call_tool("search_roles", {"permission": "inventory:hosts:read"})

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["org_version"], "v2")
        role_names = [r["name"] for r in tool_output["data"]]
        self.assertIn("Host Reader", role_names)
        self.assertNotIn("Empty Role", role_names)

    def test_search_roles_v2_username_ignored_with_guidance(self):
        """Positive: search_roles on V2 org returns ignored_filters when username is provided."""
        RoleV2.objects.create(name="V2 Role", tenant=self.tenant, type=RoleV2.Types.CUSTOM)
        response = self._call_tool("search_roles", {"username": "jdoe"})

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["org_version"], "v2")
        self.assertIn("ignored_filters", tool_output)
        self.assertIn("username", tool_output["ignored_filters"])
        self.assertEqual(tool_output["ignored_filters"]["username"]["value"], "jdoe")
        self.assertIn("role bindings", tool_output["ignored_filters"]["username"]["reason"])
        self.assertIn("list_role_bindings", tool_output["ignored_filters"]["username"]["alternative"])
        self.assertIn("jdoe", tool_output["ignored_filters"]["username"]["alternative"])


@override_settings(BYPASS_BOP_VERIFICATION=True, V2_APIS_ENABLED=True)
class MCPUnifiedGetRoleTests(MCPToolTestMixin, IdentityRequest):
    """Tests for the unified get_role MCP tool routing to V1 and V2."""

    def setUp(self):
        """Set up get_role tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )

    def tearDown(self):
        """Tear down get_role tests."""
        RoleV2.objects.all().delete()
        Permission.objects.all().delete()
        TenantMapping.objects.all().delete()
        Role.objects.all().delete()
        super().tearDown()

    def test_get_role_v1_returns_role_with_permissions(self):
        """Positive: get_role on V1 org returns role details with permissions and org_version=v1."""
        perm = Permission.objects.create(
            application="cost-management",
            resource_type="cost_model",
            verb="read",
            permission="cost-management:cost_model:read",
            tenant=self.tenant,
        )
        role = Role.objects.create(name="Cost Reader V1", tenant=self.tenant)
        access = Access.objects.create(role=role, permission=perm, tenant=self.tenant)
        Policy.objects.create(name="auto_policy", group=None, tenant=self.tenant).roles.add(role)

        response = self._call_tool("get_role", {"role_uuid": str(role.uuid)})

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["uuid"], str(role.uuid))
        self.assertEqual(tool_output["org_version"], "v1")
        self.assertIn("permissions", tool_output)

        access.delete()
        perm.delete()

    def test_get_role_v2_returns_role_with_permissions(self):
        """Positive: get_role on V2 org returns role details with permissions and org_version=v2."""
        TenantMapping.objects.create(tenant=self.tenant, v2_write_activated_at=timezone.now())

        perm = Permission.objects.create(
            application="vulnerability",
            resource_type="vulnerability",
            verb="read",
            permission="vulnerability:vulnerability:read",
            tenant=self.tenant,
        )
        role = RoleV2.objects.create(name="Vuln Reader V2", tenant=self.tenant)
        role.permissions.add(perm)

        response = self._call_tool("get_role", {"role_uuid": str(role.uuid)})

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["id"], str(role.uuid))
        self.assertEqual(tool_output["org_version"], "v2")
        self.assertIn("permissions", tool_output)


class MCPDeploymentGatingTests(MCPToolTestMixin, IdentityRequest):
    """Tests for deployment-level V2 tool gating."""

    def setUp(self):
        """Set up gating tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()

    def test_calling_v2_tool_when_v2_disabled_returns_error(self):
        """Negative: calling a V2-only tool when V2 is disabled returns a clear error."""
        response = self._call_tool("list_role_bindings")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertIn("V2 APIs", data["error"]["message"])

    def test_api_version_classification(self):
        """Verify all tools have an api_version set."""
        for tool_name, config in _TOOL_CONFIG.items():
            self.assertIn(
                config.api_version,
                (ApiVersion.UNIFIED, ApiVersion.COMMON, ApiVersion.V1, ApiVersion.V2, ApiVersion.UNVERSIONED),
                f"Tool '{tool_name}' has unexpected api_version: {config.api_version}",
            )

    def test_unified_tools_are_always_listed(self):
        """Positive: unified tools appear in tools/list regardless of V2 setting."""
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        tool_names = [t["name"] for t in response.json()["result"]["tools"]]
        self.assertIn("search_roles", tool_names)
        self.assertIn("get_role", tool_names)
        self.assertIn("check_user_permission", tool_names)


class MCPToolDescriptionOverrideTests(MCPToolTestMixin, IdentityRequest):
    """Test that Redis-backed description overrides are applied in tools/list."""

    def setUp(self):
        """Set up."""
        super().setUp()
        self.mcp_url = "/_private/_a2s/mcp/"
        self.client = APIClient()

    @patch(
        "management.mcp_views._get_all_description_overrides",
        return_value={"hello": "Overridden description for MCP"},
    )
    def test_override_appears_in_tools_list(self, mock_overrides):
        """After setting an override, tools/list returns the overridden description."""
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}
        response = self.client.post(
            self.mcp_url, data=json.dumps(body), content_type="application/json", **self.headers
        )
        self.assertEqual(response.status_code, 200)
        tools = response.json()["result"]["tools"]
        hello_tool = next(t for t in tools if t["name"] == "hello")
        self.assertEqual(hello_tool["description"], "Overridden description for MCP")

    @patch(
        "management.mcp_views._get_all_description_overrides",
        return_value={"hello": "custom hello"},
    )
    def test_non_overridden_tools_keep_defaults(self, mock_overrides):
        """Overriding one tool does not affect other tools."""
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}
        response = self.client.post(
            self.mcp_url, data=json.dumps(body), content_type="application/json", **self.headers
        )
        tools = response.json()["result"]["tools"]
        principals_tool = next(t for t in tools if t["name"] == "list_principals")
        self.assertIn("List principals", principals_tool["description"])


class MCPToolDescriptionEndpointTests(MCPToolTestMixin, IdentityRequest):
    """Test the internal endpoint for managing MCP tool description overrides."""

    def setUp(self):
        """Set up."""
        super().setUp()
        self.base_url = "/_private/api/utils/mcp_tool_descriptions/"
        self.client = APIClient()
        internal_context = self._create_request_context(self.customer_data, self.user_data, is_internal=True)
        self.internal_headers = internal_context["request"].META

        self._override_store = {}
        patcher_get_all = patch(
            "management.mcp_views._get_all_description_overrides",
            side_effect=lambda: dict(self._override_store),
        )
        patcher_get = patch(
            "management.mcp_views._get_description_override",
            side_effect=lambda name: self._override_store.get(name),
        )
        patcher_set = patch(
            "management.mcp_views._set_description_override",
            side_effect=lambda name, desc: self._override_store.__setitem__(name, desc),
        )
        patcher_delete = patch(
            "management.mcp_views._delete_description_override",
            side_effect=lambda name: self._override_store.pop(name, None),
        )
        patcher_get_all.start()
        patcher_get.start()
        patcher_set.start()
        patcher_delete.start()
        self.addCleanup(patcher_get_all.stop)
        self.addCleanup(patcher_get.stop)
        self.addCleanup(patcher_set.stop)
        self.addCleanup(patcher_delete.stop)

    def test_list_descriptions_returns_all_tools(self):
        """GET list returns all registered tools with default descriptions."""
        response = self.client.get(self.base_url, **self.internal_headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        tool_names = [t["tool_name"] for t in data["tools"]]
        self.assertIn("hello", tool_names)
        self.assertIn("list_principals", tool_names)
        hello_tool = next(t for t in data["tools"] if t["tool_name"] == "hello")
        self.assertIsNotNone(hello_tool["default_description"])
        self.assertIsNone(hello_tool["override_description"])
        self.assertEqual(hello_tool["active_description"], hello_tool["default_description"])

    def test_get_single_tool_description(self):
        """GET single tool returns its default description."""
        response = self.client.get(f"{self.base_url}hello/", **self.internal_headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["tool_name"], "hello")
        self.assertIsNone(data["override_description"])
        self.assertIn("RBAC service", data["default_description"])

    def test_set_description_override(self):
        """PUT sets description override for a tool."""
        new_desc = "Custom hello description for testing"
        response = self.client.put(
            f"{self.base_url}hello/",
            data=json.dumps({"description": new_desc}),
            content_type="application/json",
            **self.internal_headers,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["tool_name"], "hello")
        self.assertEqual(data["override_description"], new_desc)

    def test_delete_override_reverts_to_default(self):
        """DELETE removes override and reverts to default description."""
        self._override_store["hello"] = "temporary override"

        response = self.client.delete(f"{self.base_url}hello/", **self.internal_headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data["override_description"])

        response = self.client.get(f"{self.base_url}hello/", **self.internal_headers)
        data = response.json()
        self.assertIsNone(data["override_description"])

    def test_unknown_tool_returns_404(self):
        """PUT/DELETE/GET for unknown tool returns 404."""
        response = self.client.get(f"{self.base_url}nonexistent_tool/", **self.internal_headers)
        self.assertEqual(response.status_code, 404)

        response = self.client.put(
            f"{self.base_url}nonexistent_tool/",
            data=json.dumps({"description": "test"}),
            content_type="application/json",
            **self.internal_headers,
        )
        self.assertEqual(response.status_code, 404)

    def test_put_without_description_returns_400(self):
        """PUT without description field returns 400."""
        response = self.client.put(
            f"{self.base_url}hello/",
            data=json.dumps({}),
            content_type="application/json",
            **self.internal_headers,
        )
        self.assertEqual(response.status_code, 400)


@override_settings(BYPASS_BOP_VERIFICATION=True)
class MCPGetUserStateTests(MCPToolTestMixin, IdentityRequest):
    """Tests for the get_user_state MCP tool."""

    def setUp(self):
        """Set up get_user_state tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.test_username = self.user_data["username"]
        self.principal = Principal.objects.create(username=self.test_username, tenant=self.tenant)

        # Create a group and add the principal
        self.group = Group.objects.create(name="Wilson Project", description="Test project group", tenant=self.tenant)
        self.group.principals.add(self.principal)

        # Create a role with permissions
        self.role = Role.objects.create(
            name="Project Contributor", display_name="Project Contributor", tenant=self.tenant
        )
        self.permission = Permission.objects.create(
            application="project",
            resource_type="tasks",
            verb="write",
            permission="project:tasks:write",
            tenant=self.tenant,
        )
        self.access = Access.objects.create(permission=self.permission, role=self.role, tenant=self.tenant)

        # Assign role to group via policy
        self.policy = Policy.objects.create(name="project_policy", group=self.group, tenant=self.tenant)
        self.policy.roles.add(self.role)

        # Create audit log entries for actions BY the user (with resource_uuid for exact matching)
        AuditLog.objects.create(
            principal_username=self.test_username,
            resource_type=AuditLog.GROUP,
            resource_uuid=self.group.uuid,
            action=AuditLog.ADD,
            description="Added jsmith to Wilson Project",
            tenant=self.tenant,
        )
        AuditLog.objects.create(
            principal_username=self.test_username,
            resource_type=AuditLog.GROUP,
            resource_uuid=self.group.uuid,
            action=AuditLog.REMOVE,
            description="Removed old_user from Wilson Project",
            tenant=self.tenant,
        )

    def tearDown(self):
        """Tear down get_user_state tests."""
        AuditLog.objects.all().delete()
        Policy.objects.all().delete()
        Access.objects.all().delete()
        Role.objects.all().delete()
        Permission.objects.all().delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    def test_get_user_state_success(self):
        """Positive: get_user_state returns comprehensive user state."""
        response = self._call_tool("get_user_state", {"username": self.test_username})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)

        # Verify basic structure — PII redacted, uuid present
        self.assertNotIn("username", tool_output)
        self.assertEqual(tool_output["uuid"], str(self.principal.uuid))
        self.assertEqual(tool_output["org_version"], "v1")
        self.assertIn("groups", tool_output)
        self.assertIn("access", tool_output)
        self.assertIn("user_actions", tool_output)
        self.assertIn("summary", tool_output)

    def test_get_user_state_includes_groups(self):
        """Positive: get_user_state includes user's groups with roles."""
        response = self._call_tool("get_user_state", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["summary"]["group_count"], 1)
        self.assertEqual(len(tool_output["groups"]), 1)

        group = tool_output["groups"][0]
        self.assertEqual(group["name"], "Wilson Project")
        self.assertEqual(group["description"], "Test project group")
        self.assertIn("roles", group)
        self.assertEqual(len(group["roles"]), 1)
        self.assertEqual(group["roles"][0]["name"], "Project Contributor")

    def test_get_user_state_includes_access(self):
        """Positive: get_user_state includes user's access permissions."""
        response = self._call_tool("get_user_state", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        self.assertGreater(len(tool_output["access"]), 0)

        # Find the permission we created
        permissions = [a["permission"] for a in tool_output["access"]]
        self.assertIn("project:tasks:write", permissions)

    def test_get_user_state_includes_user_actions(self):
        """Positive: get_user_state includes actions performed BY the user."""
        response = self._call_tool("get_user_state", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        user_actions = tool_output["user_actions"]

        self.assertEqual(user_actions["total_count"], 2)
        self.assertIn("by_group", user_actions)
        self.assertIn("Wilson Project", user_actions["by_group"])
        self.assertEqual(len(user_actions["by_group"]["Wilson Project"]), 2)
        self.assertIn("by_type", user_actions)
        self.assertIn("group:add", user_actions["by_type"])
        self.assertIn("group:remove", user_actions["by_type"])

    def test_get_user_state_summary(self):
        """Positive: get_user_state returns correct summary counts."""
        response = self._call_tool("get_user_state", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        summary = tool_output["summary"]

        self.assertEqual(summary["group_count"], 1)
        self.assertGreaterEqual(summary["permission_count"], 1)
        self.assertEqual(summary["actions_by_user"], 2)

    def test_get_user_state_user_not_found(self):
        """Negative: get_user_state returns error for non-existent user."""
        response = self._call_tool("get_user_state", {"username": "nonexistent_user"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)

        self.assertIn("error", tool_output)
        self.assertIn("not found", tool_output["error"])
        self.assertIn("hint", tool_output)

    @patch("management.mcp_views._list_principals_by_name")
    def test_get_user_state_resolves_display_name(self, mock_by_name):
        """Positive: get_user_state auto-resolves a display name to a username."""
        mock_by_name.return_value = json.dumps(
            {
                "data": [
                    {"username": self.test_username, "first_name": "Test", "last_name": "User"},
                ]
            }
        )
        response = self._call_tool("get_user_state", {"username": "Test User"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)

        self.assertNotIn("error", tool_output)
        self.assertNotIn("username", tool_output)
        self.assertEqual(tool_output["uuid"], str(self.principal.uuid))
        self.assertIn("groups", tool_output)

    @patch("management.mcp_views._list_principals_by_name")
    def test_get_user_state_multiple_name_matches(self, mock_by_name):
        """Negative: get_user_state returns candidates when multiple users match."""
        mock_by_name.return_value = json.dumps(
            {
                "data": [
                    {"username": "jdoe", "first_name": "John", "last_name": "Doe"},
                    {"username": "jsmith", "first_name": "John", "last_name": "Smith"},
                ]
            }
        )
        response = self._call_tool("get_user_state", {"username": "John"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)

        self.assertIn("error", tool_output)
        self.assertIn("Multiple users match", tool_output["error"])
        self.assertEqual(len(tool_output["candidates"]), 2)

    def test_get_user_state_without_auth_returns_error(self):
        """Permission: get_user_state without auth returns auth error."""
        response = self._call_tool("get_user_state", {"username": self.test_username}, use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_get_user_state_without_group_roles(self):
        """Positive: get_user_state with include_group_roles=false omits roles."""
        response = self._call_tool(
            "get_user_state",
            {"username": self.test_username, "include_group_roles": False},
        )

        tool_output = self._get_tool_output(response)
        group = tool_output["groups"][0]
        self.assertNotIn("roles", group)
        self.assertNotIn("role_count", group)

    def test_get_user_state_without_permissions(self):
        """Positive: get_user_state with include_permissions=false omits access."""
        response = self._call_tool(
            "get_user_state",
            {"username": self.test_username, "include_permissions": False},
        )

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["access"]), 0)
        self.assertEqual(tool_output["summary"]["permission_count"], 0)

    def test_get_user_state_audit_log_limit(self):
        """Positive: get_user_state respects audit_log_limit parameter."""
        # Add more audit log entries with resource_uuid for exact matching
        for i in range(5):
            AuditLog.objects.create(
                principal_username=self.test_username,
                resource_type=AuditLog.GROUP,
                resource_uuid=self.group.uuid,
                action=AuditLog.ADD,
                description=f"Added user{i} to Wilson Project",
                tenant=self.tenant,
            )

        response = self._call_tool(
            "get_user_state",
            {"username": self.test_username, "audit_log_limit": 3},
        )

        tool_output = self._get_tool_output(response)
        # recent should be limited to 3
        self.assertLessEqual(len(tool_output["user_actions"]["recent"]), 3)

    def test_get_user_state_multiple_groups(self):
        """Positive: get_user_state handles user in multiple groups."""
        # Create another group
        group2 = Group.objects.create(name="Platform Default", tenant=self.tenant)
        group2.principals.add(self.principal)

        response = self._call_tool("get_user_state", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["summary"]["group_count"], 2)
        group_names = [g["name"] for g in tool_output["groups"]]
        self.assertIn("Wilson Project", group_names)
        self.assertIn("Platform Default", group_names)

    def test_get_user_state_includes_hints(self):
        """Positive: get_user_state includes helpful hints for further investigation."""
        response = self._call_tool("get_user_state", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        self.assertIn("hints", tool_output)
        self.assertIn("check_specific_permission", tool_output["hints"])
        self.assertIn("view_audit_details", tool_output["hints"])
        self.assertIn("trace_role_permissions", tool_output["hints"])

    @override_settings(MCP_PII_REDACTION_ENABLED=False)
    def test_get_user_state_pii_visible_when_redaction_disabled(self):
        """Positive: get_user_state returns username instead of uuid when redaction is disabled via env var."""
        response = self._call_tool("get_user_state", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        self.assertIn("username", tool_output)
        self.assertEqual(tool_output["username"], self.test_username)
        self.assertNotIn("uuid", tool_output)

    def test_get_user_state_description_contains_offboarding_keywords(self):
        """Positive: get_user_state tool description contains offboarding trigger keywords."""
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)
        tools = response.json()["result"]["tools"]
        tool = next(t for t in tools if t["name"] == "get_user_state")
        description = tool["description"]

        self.assertIn("offboard", description)
        self.assertIn("contractor", description.lower())
        self.assertIn("compliance report", description)
        self.assertIn("AFTER ANALYSIS", description)


class MCPLookupPersonTests(MCPToolTestMixin, IdentityRequest):
    """Tests for the lookup_person MCP tool (forwarding alias for get_user_state)."""

    def setUp(self):
        """Set up lookup_person tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.test_username = self.user_data["username"]
        self.principal = Principal.objects.create(username=self.test_username, tenant=self.tenant)

        self.group = Group.objects.create(
            name="Contractor Team", description="External contractors", tenant=self.tenant
        )
        self.group.principals.add(self.principal)

        self.role = Role.objects.create(name="Viewer", display_name="Viewer", tenant=self.tenant)
        self.permission = Permission.objects.create(
            application="inventory",
            resource_type="hosts",
            verb="read",
            permission="inventory:hosts:read",
            tenant=self.tenant,
        )
        self.access = Access.objects.create(permission=self.permission, role=self.role, tenant=self.tenant)

        self.policy = Policy.objects.create(name="contractor_policy", group=self.group, tenant=self.tenant)
        self.policy.roles.add(self.role)

    def tearDown(self):
        """Tear down lookup_person tests."""
        Policy.objects.all().delete()
        Access.objects.all().delete()
        Role.objects.all().delete()
        Permission.objects.all().delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    def test_lookup_person_registered_in_tools_list(self):
        """Positive: lookup_person appears in the MCP tools/list response."""
        tool_names = self._get_tool_names()
        self.assertIn("lookup_person", tool_names)

    def test_lookup_person_description_contains_contractor_keywords(self):
        """Positive: lookup_person description contains contractor/offboarding keywords."""
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)
        tools = response.json()["result"]["tools"]
        tool = next(t for t in tools if t["name"] == "lookup_person")
        description = tool["description"]

        self.assertIn("contractor", description)
        self.assertIn("vendor", description)
        self.assertIn("consultant", description)
        self.assertIn("offboarding", description)

    def test_lookup_person_returns_same_data_as_get_user_state(self):
        """Positive: lookup_person returns identical output to get_user_state."""
        response_person = self._call_tool("lookup_person", {"username": self.test_username})
        response_state = self._call_tool("get_user_state", {"username": self.test_username})

        output_person = self._get_tool_output(response_person)
        output_state = self._get_tool_output(response_state)

        self.assertEqual(output_person["uuid"], output_state["uuid"])
        self.assertEqual(output_person["org_version"], output_state["org_version"])
        self.assertEqual(output_person["summary"]["group_count"], output_state["summary"]["group_count"])
        self.assertEqual(output_person["summary"]["permission_count"], output_state["summary"]["permission_count"])

    def test_lookup_person_success(self):
        """Positive: lookup_person returns comprehensive user state."""
        response = self._call_tool("lookup_person", {"username": self.test_username})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)

        self.assertEqual(tool_output["uuid"], str(self.principal.uuid))
        self.assertIn("groups", tool_output)
        self.assertIn("access", tool_output)
        self.assertIn("summary", tool_output)

        self.assertEqual(tool_output["summary"]["group_count"], 1)
        self.assertEqual(tool_output["groups"][0]["name"], "Contractor Team")

    def test_lookup_person_user_not_found(self):
        """Negative: lookup_person returns error for non-existent user."""
        response = self._call_tool("lookup_person", {"username": "nonexistent_contractor"})

        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("not found", tool_output["error"])

    def test_lookup_person_without_auth_returns_error(self):
        """Permission: lookup_person without auth returns auth error."""
        response = self._call_tool("lookup_person", {"username": self.test_username}, use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)


@override_settings(BYPASS_BOP_VERIFICATION=True, V2_APIS_ENABLED=True)
class MCPGetUserStateV2Tests(MCPToolTestMixin, IdentityRequest):
    """Tests for get_user_state on V2 organizations."""

    def setUp(self):
        """Set up V2 get_user_state tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.test_username = self.user_data["username"]
        self.principal = Principal.objects.create(username=self.test_username, tenant=self.tenant)

        self.enterContext(
            patch(
                "management.permissions.role_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )

        # Activate V2 for this tenant
        TenantMapping.objects.create(tenant=self.tenant, v2_write_activated_at=timezone.now())

        # Create a group and add the principal
        self.group = Group.objects.create(name="V2 Test Group", tenant=self.tenant)
        self.group.principals.add(self.principal)

        # Create V2 role with permission
        self.v2_perm = Permission.objects.create(
            application="inventory",
            resource_type="hosts",
            verb="read",
            permission="inventory:hosts:read",
            tenant=self.tenant,
        )
        self.v2_role = RoleV2.objects.create(name="Host Reader", tenant=self.tenant)
        self.v2_role.permissions.add(self.v2_perm)

        # Create role binding
        self.binding = RoleBinding.objects.create(
            tenant=self.tenant,
            role=self.v2_role,
            resource_type="workspace",
            resource_id="root-workspace-id",
        )
        RoleBindingPrincipal.objects.create(binding=self.binding, principal=self.principal, source="direct")

    def tearDown(self):
        """Tear down V2 get_user_state tests."""
        RoleBindingPrincipal.objects.all().delete()
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        RoleV2.objects.all().delete()
        Permission.objects.all().delete()
        TenantMapping.objects.all().delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    def test_get_user_state_v2_returns_org_version(self):
        """Positive: get_user_state on V2 org returns org_version=v2."""
        response = self._call_tool("get_user_state", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["org_version"], "v2")

    def test_get_user_state_v2_includes_role_binding_permissions(self):
        """Positive: get_user_state on V2 org includes permissions from role bindings."""
        response = self._call_tool("get_user_state", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        self.assertGreater(len(tool_output["access"]), 0)

        # Find the V2 permission
        permissions = [a["permission"] for a in tool_output["access"]]
        self.assertIn("inventory:hosts:read", permissions)

        # Verify role info is included
        access_entry = next(a for a in tool_output["access"] if a["permission"] == "inventory:hosts:read")
        self.assertEqual(access_entry["role_name"], "Host Reader")

    def test_get_user_state_v2_includes_resource_scope(self):
        """Positive: get_user_state on V2 org includes resource scope in access."""
        response = self._call_tool("get_user_state", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        access_entry = next(a for a in tool_output["access"] if a["permission"] == "inventory:hosts:read")

        self.assertIn("resource_scope", access_entry)
        self.assertEqual(access_entry["resource_scope"]["type"], "workspace")
        self.assertEqual(access_entry["resource_scope"]["id"], "root-workspace-id")

    def test_get_user_state_v2_includes_group_based_role_bindings(self):
        """Positive: get_user_state on V2 org includes permissions from group-based role bindings."""
        group_perm = Permission.objects.create(
            application="cost-management",
            resource_type="cost_model",
            verb="write",
            permission="cost-management:cost_model:write",
            tenant=self.tenant,
        )
        group_role = RoleV2.objects.create(name="Cost Manager", tenant=self.tenant)
        group_role.permissions.add(group_perm)

        group_binding = RoleBinding.objects.create(
            tenant=self.tenant,
            role=group_role,
            resource_type="workspace",
            resource_id="group-workspace-id",
        )
        RoleBindingGroup.objects.create(binding=group_binding, group=self.group)

        response = self._call_tool("get_user_state", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        permissions = [a["permission"] for a in tool_output["access"]]
        self.assertIn("cost-management:cost_model:write", permissions)

        access_entry = next(a for a in tool_output["access"] if a["permission"] == "cost-management:cost_model:write")
        self.assertEqual(access_entry["role_name"], "Cost Manager")
        self.assertEqual(access_entry["resource_scope"]["type"], "workspace")
        self.assertEqual(access_entry["resource_scope"]["id"], "group-workspace-id")

    def test_get_user_state_v2_multi_scope_bindings_preserved(self):
        """Positive: Same permission on different scopes is returned for each scope."""
        binding_b = RoleBinding.objects.create(
            tenant=self.tenant,
            role=self.v2_role,
            resource_type="workspace",
            resource_id="workspace-b-id",
        )
        RoleBindingPrincipal.objects.create(binding=binding_b, principal=self.principal, source="direct")

        response = self._call_tool("get_user_state", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        hosts_read_entries = [a for a in tool_output["access"] if a["permission"] == "inventory:hosts:read"]
        self.assertEqual(len(hosts_read_entries), 2)

        scope_ids = {e["resource_scope"]["id"] for e in hosts_read_entries}
        self.assertEqual(scope_ids, {"root-workspace-id", "workspace-b-id"})


class MCPTimeoutTests(MCPToolTestMixin, IdentityRequest):
    """Test MCP tool execution timeout behavior."""

    def setUp(self):
        """Set up timeout tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()

    # --- _execute_with_timeout unit tests ---

    def test_execute_with_timeout_returns_result(self):
        """Positive: Fast function returns its result within timeout."""
        result = _execute_with_timeout(lambda: "ok", timeout=5)
        self.assertEqual(result, "ok")

    def test_execute_with_timeout_passes_args(self):
        """Positive: Arguments are forwarded to the function."""
        result = _execute_with_timeout(lambda x, y: x + y, 5, 3, 4)
        self.assertEqual(result, 7)

    def test_execute_with_timeout_passes_kwargs(self):
        """Positive: Keyword arguments are forwarded to the function."""
        result = _execute_with_timeout(lambda name="world": f"hello {name}", 5, name="test")
        self.assertEqual(result, "hello test")

    def test_execute_with_timeout_raises_on_slow_function(self):
        """Negative: Slow function raises ToolTimeoutError."""
        with self.assertRaises(ToolTimeoutError):
            _execute_with_timeout(lambda: time.sleep(0.5), timeout=0.1)

    def test_execute_with_timeout_propagates_exceptions(self):
        """Negative: Exceptions from the function propagate unchanged."""

        def raise_value_error():
            raise ValueError("test error")

        with self.assertRaises(ValueError):
            _execute_with_timeout(raise_value_error, timeout=5)

    # --- Integration tests via MCP endpoint ---

    @override_settings(MCP_TOOL_TIMEOUT_SECONDS=0.1)
    @patch("management.mcp_views._TOOL_CONFIG")
    def test_tool_call_returns_timeout_error(self, mock_config):
        """Negative: Slow tool returns JSON-RPC timeout error."""
        mock_config.get.return_value = ToolConfig(
            fn=lambda: time.sleep(0.5),
            requires_auth=False,
            passes_request=False,
        )
        response = self._call_tool("slow_tool")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32603)
        self.assertEqual(data["error"]["message"], "Tool execution timed out after 0.1s")

    @override_settings(MCP_TOOL_TIMEOUT_SECONDS=0.1)
    @patch("management.mcp_views._TOOL_CONFIG")
    def test_tool_call_timeout_with_passes_request(self, mock_config):
        """Negative: Slow tool with passes_request=True also times out correctly."""
        mock_config.get.return_value = ToolConfig(
            fn=lambda request: time.sleep(0.5),
            requires_auth=False,
            passes_request=True,
        )
        response = self._call_tool("slow_auth_tool")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32603)
        self.assertIn("timed out", data["error"]["message"])

    @override_settings(MCP_TOOL_TIMEOUT_SECONDS=0.1)
    @patch("management.mcp_views._TOOL_CONFIG")
    def test_timeout_records_prometheus_metric(self, mock_config):
        """Positive: Timeout records metric with status='timeout'."""
        mock_config.get.return_value = ToolConfig(
            fn=lambda: time.sleep(0.5),
            requires_auth=False,
            passes_request=False,
        )

        with patch("management.mcp_views._record_metric") as mock_metric:
            self._call_tool("slow_tool")
            mock_metric.assert_called_once()
            args = mock_metric.call_args[0]
            self.assertEqual(args[0], "slow_tool")
            self.assertEqual(args[1], "timeout")

    def test_hello_skips_timeout_tracking(self):
        """Edge case: hello tool still works and skips metric tracking."""
        response = self._call_tool("hello")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("result", data)
        self.assertFalse(data["result"]["isError"])

    @override_settings(MCP_TOOL_TIMEOUT_SECONDS=30)
    def test_default_timeout_allows_normal_tools(self):
        """Positive: Normal tools complete within default timeout."""
        response = self._call_tool("hello")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("result", data)
        self.assertFalse(data["result"]["isError"])


@override_settings(BYPASS_BOP_VERIFICATION=True)
class MCPInvestigateTamAccessTests(MCPToolTestMixin, IdentityRequest):
    """Tests for the investigate_tam_access MCP tool."""

    def setUp(self):
        """Set up investigate_tam_access tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()

        # Create a public tenant for system roles
        self.public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")

        # Create a system role with permissions
        self.system_role = Role.objects.create(
            name="Subscriptions viewer",
            display_name="Subscriptions viewer",
            description="View subscription data",
            system=True,
            tenant=self.public_tenant,
        )
        self.perm = Permission.objects.create(
            application="subscriptions",
            resource_type="products",
            verb="read",
            permission="subscriptions:products:read",
            tenant=self.public_tenant,
        )
        self.access = Access.objects.create(permission=self.perm, role=self.system_role, tenant=self.public_tenant)

        # Create a cross-account request
        self.car = CrossAccountRequest.objects.create(
            target_org=self.customer_data["org_id"],
            user_id="12345",
            status="approved",
            start_date=timezone.now() - timezone.timedelta(days=5),
            end_date=timezone.now() + timezone.timedelta(days=6),
        )
        self.car.roles.add(self.system_role)

    def tearDown(self):
        """Tear down investigate_tam_access tests."""
        CrossAccountRequest.objects.all().delete()
        Access.objects.filter(tenant=self.public_tenant).delete()
        Permission.objects.filter(tenant=self.public_tenant).delete()
        Role.objects.filter(tenant=self.public_tenant).delete()
        super().tearDown()

    def test_investigate_tam_access_success(self):
        """Positive: investigate_tam_access returns cross-account request data."""
        response = self._call_tool("investigate_tam_access")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("requests", tool_output)
        self.assertIn("analysis", tool_output)

    def test_investigate_tam_access_without_auth_returns_error(self):
        """Permission: investigate_tam_access without auth returns auth error."""
        response = self._call_tool("investigate_tam_access", use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_investigate_tam_access_returns_roles_and_permissions(self, mock_proxy):
        """Positive: investigate_tam_access returns roles and their permissions."""
        mock_proxy.return_value = {
            "status_code": 200,
            "data": [
                {
                    "user_id": "12345",
                    "first_name": "Rachel",
                    "last_name": "TAM",
                    "email": "rachel@redhat.com",
                    "username": "rtam",
                }
            ],
        }

        response = self._call_tool("investigate_tam_access")

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["requests"]), 1)
        request_data = tool_output["requests"][0]
        self.assertEqual(request_data["status"], "approved")
        self.assertIn("roles", request_data)
        self.assertEqual(len(request_data["roles"]), 1)
        self.assertEqual(request_data["roles"][0]["display_name"], "Subscriptions viewer")
        self.assertIn("subscriptions:products:read", request_data["roles"][0]["permissions"])

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_investigate_tam_access_filter_by_name(self, mock_proxy):
        """Positive: investigate_tam_access filters by requester name."""
        mock_proxy.return_value = {
            "status_code": 200,
            "data": [
                {
                    "user_id": "12345",
                    "first_name": "Rachel",
                    "last_name": "TAM",
                    "email": "rachel@redhat.com",
                    "username": "rtam",
                }
            ],
        }

        response = self._call_tool("investigate_tam_access", {"requester_name": "Rachel"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["requests"]), 1)

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_investigate_tam_access_filter_by_name_no_match(self, mock_proxy):
        """Negative: investigate_tam_access returns empty when name doesn't match."""
        mock_proxy.return_value = {
            "status_code": 200,
            "data": [
                {
                    "user_id": "12345",
                    "first_name": "Rachel",
                    "last_name": "TAM",
                    "email": "rachel@redhat.com",
                    "username": "rtam",
                }
            ],
        }

        response = self._call_tool("investigate_tam_access", {"requester_name": "John"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["requests"]), 0)
        self.assertIn("filtered_count", tool_output["analysis"])
        self.assertIn("lifecycle", tool_output["analysis"])

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_investigate_tam_access_required_permission_found(self, mock_proxy):
        """Positive: investigate_tam_access identifies when required permission is granted."""
        mock_proxy.return_value = {"status_code": 200, "data": []}

        response = self._call_tool(
            "investigate_tam_access",
            {"required_permission": "subscriptions:products:read"},
        )

        tool_output = self._get_tool_output(response)
        self.assertIn("required_permission_check", tool_output["analysis"])
        self.assertTrue(tool_output["analysis"]["required_permission_check"]["granted"])
        self.assertEqual(
            tool_output["analysis"]["required_permission_check"]["via_role"],
            "Subscriptions viewer",
        )

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_investigate_tam_access_required_permission_not_found(self, mock_proxy):
        """Negative: investigate_tam_access identifies when required permission is NOT granted."""
        mock_proxy.return_value = {"status_code": 200, "data": []}

        response = self._call_tool(
            "investigate_tam_access",
            {"required_permission": "subscriptions:watch:read"},
        )

        tool_output = self._get_tool_output(response)
        self.assertIn("required_permission_check", tool_output["analysis"])
        self.assertFalse(tool_output["analysis"]["required_permission_check"]["granted"])
        self.assertIn("similar_permissions_granted", tool_output["analysis"]["required_permission_check"])
        self.assertIn(
            "subscriptions:products:read",
            tool_output["analysis"]["required_permission_check"]["similar_permissions_granted"],
        )

    def test_investigate_tam_access_no_requests(self):
        """Negative: investigate_tam_access returns enriched response when no requests exist."""
        # Delete the test cross-account request
        self.car.delete()

        response = self._call_tool("investigate_tam_access")

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["requests"]), 0)
        self.assertIn("message", tool_output["analysis"])
        self.assertIn("hint", tool_output["analysis"])
        self.assertIn("other_statuses", tool_output["analysis"])
        self.assertIn("pending", tool_output["analysis"]["other_statuses"])
        self.assertIn("expired", tool_output["analysis"]["other_statuses"])
        self.assertIn("lifecycle", tool_output["analysis"])
        self.assertIn("create", tool_output["analysis"]["lifecycle"])

    def test_investigate_tam_access_no_requests_with_pending(self):
        """Positive: investigate_tam_access shows pending count when no approved requests exist."""
        self.car.status = "pending"
        self.car.save()

        response = self._call_tool("investigate_tam_access")

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["requests"]), 0)
        self.assertEqual(tool_output["analysis"]["other_statuses"]["pending"], 1)
        self.assertIn("pending", tool_output["analysis"]["hint"])

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_investigate_tam_access_shows_days_remaining(self, mock_proxy):
        """Positive: investigate_tam_access shows days remaining for approved requests."""
        mock_proxy.return_value = {"status_code": 200, "data": []}

        response = self._call_tool("investigate_tam_access")

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["requests"]), 1)
        self.assertIn("days_remaining", tool_output["requests"][0])
        # Allow 5 or 6 days due to timing between test setup and tool execution
        self.assertIn(tool_output["requests"][0]["days_remaining"], [5, 6])

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_investigate_tam_access_permissions_by_application(self, mock_proxy):
        """Positive: investigate_tam_access groups permissions by application."""
        mock_proxy.return_value = {"status_code": 200, "data": []}

        response = self._call_tool("investigate_tam_access")

        tool_output = self._get_tool_output(response)
        self.assertIn("permissions_by_application", tool_output["analysis"])
        self.assertIn("subscriptions", tool_output["analysis"]["permissions_by_application"])


class AuditRedhatAccessTests(MCPToolTestMixin, IdentityRequest):
    """Tests for the audit_redhat_access MCP tool."""

    def setUp(self):
        """Set up audit_redhat_access tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()

        # Create a public tenant for system roles
        self.public_tenant, _ = Tenant.objects.get_or_create(tenant_name="public")

        # Create system roles with permissions
        self.system_role = Role.objects.create(
            name="Test role 1",
            display_name="Test role 1",
            description="Test role for audit tests",
            system=True,
            tenant=self.public_tenant,
        )
        self.perm = Permission.objects.create(
            application="test-app",
            resource_type="resources",
            verb="read",
            permission="test-app:resources:read",
            tenant=self.public_tenant,
        )
        Access.objects.create(permission=self.perm, role=self.system_role, tenant=self.public_tenant)

        # Create another role
        self.other_role = Role.objects.create(
            name="Test role 2",
            display_name="Test role 2",
            description="Another test role",
            system=True,
            tenant=self.public_tenant,
        )
        self.other_perm = Permission.objects.create(
            application="other-app",
            resource_type="items",
            verb="read",
            permission="other-app:items:read",
            tenant=self.public_tenant,
        )
        Access.objects.create(permission=self.other_perm, role=self.other_role, tenant=self.public_tenant)

        # Create a cross-account request (not expiring soon)
        self.car = CrossAccountRequest.objects.create(
            target_org=self.tenant.org_id,
            user_id="10001",
            status="approved",
            start_date=timezone.now() - timezone.timedelta(days=1),
            end_date=timezone.now() + timezone.timedelta(days=30),
        )
        self.car.roles.add(self.system_role)

        # Create another request expiring soon
        self.car_expiring = CrossAccountRequest.objects.create(
            target_org=self.tenant.org_id,
            user_id="10002",
            status="approved",
            start_date=timezone.now() - timezone.timedelta(days=10),
            end_date=timezone.now() + timezone.timedelta(days=2),
        )
        self.car_expiring.roles.add(self.other_role)

    def tearDown(self):
        """Tear down audit_redhat_access tests."""
        CrossAccountRequest.objects.all().delete()
        AuditLog.objects.all().delete()
        Access.objects.filter(tenant=self.public_tenant).delete()
        Permission.objects.filter(tenant=self.public_tenant).delete()
        Role.objects.filter(tenant=self.public_tenant).delete()
        super().tearDown()

    def test_audit_redhat_access_success(self):
        """Positive: audit_redhat_access returns cross-account data with summary."""
        response = self._call_tool("audit_redhat_access")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)
        self.assertIn("active_access", tool_output)
        self.assertIn("summary", tool_output)

    def test_audit_redhat_access_without_auth_returns_error(self):
        """Permission: audit_redhat_access without auth returns auth error."""
        response = self._call_tool("audit_redhat_access", use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_audit_redhat_access_returns_user_info(self, mock_proxy):
        """Positive: audit_redhat_access returns user information from BOP."""
        mock_proxy.return_value = {
            "status_code": 200,
            "data": [
                {
                    "user_id": "10001",
                    "first_name": "Test",
                    "last_name": "User1",
                    "email": "user1@example.com",
                    "username": "testuser1",
                },
                {
                    "user_id": "10002",
                    "first_name": "Test",
                    "last_name": "User2",
                    "email": "user2@example.com",
                    "username": "testuser2",
                },
            ],
        }

        response = self._call_tool("audit_redhat_access")

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["active_access"]), 2)

        # PII is redacted — user_info contains only user_id
        user1 = next(u for u in tool_output["active_access"] if u["user_info"]["user_id"] == "10001")
        self.assertNotIn("name", user1["user_info"])
        self.assertNotIn("email", user1["user_info"])
        self.assertIn("Test role 1", [r["name"] for r in user1["roles"]])

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_audit_redhat_access_shows_expiring_soon(self, mock_proxy):
        """Positive: audit_redhat_access identifies access expiring within 7 days."""
        mock_proxy.return_value = {"status_code": 200, "data": []}

        response = self._call_tool("audit_redhat_access")

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["summary"]["expiring_soon"], 1)
        self.assertIn("warning", tool_output["summary"])

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_audit_redhat_access_with_audit_activity(self, mock_proxy):
        """Positive: audit_redhat_access includes audit log activity."""
        mock_proxy.return_value = {
            "status_code": 200,
            "data": [
                {
                    "user_id": "10001",
                    "first_name": "Test",
                    "last_name": "User1",
                    "email": "user1@example.com",
                    "username": "testuser1",
                }
            ],
        }

        # Create some audit log entries for this user
        AuditLog.objects.create(
            principal_username="testuser1",
            action="edit",
            resource_type="group",
            description="Edited group: Test Group",
            tenant=self.tenant,
        )
        AuditLog.objects.create(
            principal_username="testuser1",
            action="add",
            resource_type="user",
            description="Added user to group",
            tenant=self.tenant,
        )

        response = self._call_tool("audit_redhat_access")

        tool_output = self._get_tool_output(response)
        user_access = tool_output["active_access"][0]
        self.assertEqual(user_access["audit_activity"]["total_actions"], 2)
        self.assertIn("edit", user_access["audit_activity"]["summary"])
        self.assertIn("add", user_access["audit_activity"]["summary"])

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_audit_redhat_access_identifies_unused_access(self, mock_proxy):
        """Positive: audit_redhat_access identifies users with no audit activity."""
        mock_proxy.return_value = {
            "status_code": 200,
            "data": [
                {
                    "user_id": "10001",
                    "first_name": "Test",
                    "last_name": "User1",
                    "email": "user1@example.com",
                    "username": "testuser1",
                },
                {
                    "user_id": "10002",
                    "first_name": "Test",
                    "last_name": "User2",
                    "email": "user2@example.com",
                    "username": "testuser2",
                },
            ],
        }

        response = self._call_tool("audit_redhat_access")

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["summary"]["unused_access"], 2)
        self.assertIn("note", tool_output["summary"])

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_audit_redhat_access_permissions_by_application(self, mock_proxy):
        """Positive: audit_redhat_access groups permissions by application."""
        mock_proxy.return_value = {"status_code": 200, "data": []}

        response = self._call_tool("audit_redhat_access")

        tool_output = self._get_tool_output(response)
        self.assertIn("permissions_by_application", tool_output["summary"])
        self.assertIn("test-app", tool_output["summary"]["permissions_by_application"])
        self.assertIn("other-app", tool_output["summary"]["permissions_by_application"])

    def test_audit_redhat_access_no_requests(self):
        """Negative: audit_redhat_access returns enriched response when no requests exist."""
        CrossAccountRequest.objects.all().delete()

        response = self._call_tool("audit_redhat_access")

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["active_access"]), 0)
        self.assertIn("message", tool_output["summary"])
        self.assertIn("ciso_briefing", tool_output["summary"])
        self.assertIn("Zero Red Hat personnel", tool_output["summary"]["ciso_briefing"])
        self.assertIn("briefing_template", tool_output["summary"])
        self.assertIn("monitoring", tool_output["summary"])
        self.assertIn("pending_requests", tool_output["summary"])
        self.assertIn("expired_requests", tool_output["summary"])

    def test_audit_redhat_access_no_active_with_pending(self):
        """Positive: audit_redhat_access shows pending count when no active access exists."""
        # Change existing request to pending
        for car in CrossAccountRequest.objects.all():
            car.status = "pending"
            car.save()

        response = self._call_tool("audit_redhat_access")

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["active_access"]), 0)
        self.assertGreaterEqual(tool_output["summary"]["pending_requests"], 1)
        self.assertIn("pending", tool_output["summary"]["ciso_briefing"])

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_audit_redhat_access_include_inactive(self, mock_proxy):
        """Positive: audit_redhat_access includes expired requests when include_inactive=true."""
        mock_proxy.return_value = {"status_code": 200, "data": []}

        # Create an expired request
        expired_car = CrossAccountRequest.objects.create(
            target_org=self.tenant.org_id,
            user_id="10003",
            status="expired",
            start_date=timezone.now() - timezone.timedelta(days=30),
            end_date=timezone.now() - timezone.timedelta(days=1),
        )
        expired_car.roles.add(self.system_role)

        response = self._call_tool("audit_redhat_access", {"include_inactive": True})

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["active_access"]), 3)

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_audit_redhat_access_custom_audit_days(self, mock_proxy):
        """Positive: audit_redhat_access respects custom audit_days parameter."""
        mock_proxy.return_value = {
            "status_code": 200,
            "data": [
                {
                    "user_id": "10001",
                    "first_name": "Test",
                    "last_name": "User1",
                    "email": "user1@example.com",
                    "username": "testuser1",
                }
            ],
        }

        response = self._call_tool("audit_redhat_access", {"audit_days": 7})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["summary"]["audit_period_days"], 7)

    @override_settings(MCP_PII_REDACTION_ENABLED=False)
    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_audit_redhat_access_pii_visible_when_redaction_disabled(self, mock_proxy):
        """Positive: audit_redhat_access returns full user_info when redaction is disabled via env var."""
        mock_proxy.return_value = {
            "status_code": 200,
            "data": [
                {
                    "user_id": "10001",
                    "first_name": "Test",
                    "last_name": "User1",
                    "email": "user1@example.com",
                    "username": "testuser1",
                },
            ],
        }

        response = self._call_tool("audit_redhat_access")

        tool_output = self._get_tool_output(response)
        user1 = next(u for u in tool_output["active_access"] if u["user_info"]["user_id"] == "10001")
        self.assertIn("name", user1["user_info"])
        self.assertIn("email", user1["user_info"])
        self.assertEqual(user1["user_info"]["email"], "user1@example.com")


class MCPCheckRolePermissionsTests(MCPToolTestMixin, IdentityRequest):
    """Tests for the check_role_permissions MCP tool (pre-flight role check)."""

    def setUp(self):
        """Set up check_role_permissions tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()

        # Create permissions for testing
        self.patch_read_perm = Permission.objects.create(
            application="patch",
            resource_type="*",
            verb="read",
            permission="patch:*:read",
            tenant=self.tenant,
        )
        self.remediation_read_perm = Permission.objects.create(
            application="remediations",
            resource_type="*",
            verb="read",
            permission="remediations:*:read",
            tenant=self.tenant,
        )
        self.patch_write_perm = Permission.objects.create(
            application="patch",
            resource_type="systems",
            verb="write",
            permission="patch:systems:write",
            tenant=self.tenant,
        )

    def tearDown(self):
        """Tear down check_role_permissions tests."""
        Access.objects.all().delete()
        Role.objects.all().delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    def test_check_role_permissions_success(self):
        """Positive: check_role_permissions returns comprehensive role analysis."""
        role = Role.objects.create(name="Patch Reviewer", tenant=self.tenant)
        Access.objects.create(role=role, permission=self.patch_read_perm, tenant=self.tenant)
        Access.objects.create(role=role, permission=self.remediation_read_perm, tenant=self.tenant)

        response = self._call_tool("check_role_permissions", {"role_name": "Patch Reviewer"})

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)

        # Verify role info
        self.assertEqual(tool_output["role"]["name"], "Patch Reviewer")
        self.assertIn("uuid", tool_output["role"])

        # Verify permissions structure
        self.assertEqual(tool_output["permissions"]["total_count"], 2)
        self.assertIn("patch", tool_output["permissions"]["by_application"])
        self.assertIn("remediations", tool_output["permissions"]["by_application"])
        self.assertIn("read", tool_output["permissions"]["verbs_included"])
        self.assertIn("write", tool_output["permissions"]["verbs_not_included"])

        # Verify coverage analysis
        self.assertTrue(tool_output["coverage_analysis"]["is_read_only"])
        self.assertFalse(tool_output["coverage_analysis"]["can_modify"])

        # Verify recommendations
        self.assertIn("recommendations", tool_output)

    def test_check_role_permissions_identifies_read_only(self):
        """Positive: check_role_permissions correctly identifies read-only roles."""
        role = Role.objects.create(name="Viewer Role", tenant=self.tenant)
        Access.objects.create(role=role, permission=self.patch_read_perm, tenant=self.tenant)

        response = self._call_tool("check_role_permissions", {"role_name": "Viewer Role"})

        tool_output = self._get_tool_output(response)
        self.assertTrue(tool_output["coverage_analysis"]["is_read_only"])
        self.assertFalse(tool_output["coverage_analysis"]["can_modify"])
        self.assertIn("read-only", tool_output["recommendations"][0].lower())

    def test_check_role_permissions_identifies_write_capability(self):
        """Positive: check_role_permissions correctly identifies roles with write access."""
        role = Role.objects.create(name="Patch Admin", tenant=self.tenant)
        Access.objects.create(role=role, permission=self.patch_read_perm, tenant=self.tenant)
        Access.objects.create(role=role, permission=self.patch_write_perm, tenant=self.tenant)

        response = self._call_tool("check_role_permissions", {"role_name": "Patch Admin"})

        tool_output = self._get_tool_output(response)
        self.assertFalse(tool_output["coverage_analysis"]["is_read_only"])
        self.assertTrue(tool_output["coverage_analysis"]["can_modify"])
        self.assertIn("write", tool_output["permissions"]["verbs_included"])

    def test_check_role_permissions_expands_wildcards(self):
        """Positive: check_role_permissions expands wildcard permissions in explanation."""
        role = Role.objects.create(name="Full Access", tenant=self.tenant)
        Access.objects.create(role=role, permission=self.patch_read_perm, tenant=self.tenant)

        response = self._call_tool("check_role_permissions", {"role_name": "Full Access"})

        tool_output = self._get_tool_output(response)
        self.assertIn("expanded_permissions", tool_output["permissions"])
        expanded = tool_output["permissions"]["expanded_permissions"]
        self.assertTrue(any("all patch resources" in exp for exp in expanded))

    def test_check_role_permissions_role_not_found(self):
        """Negative: check_role_permissions returns error for non-existent role."""
        response = self._call_tool("check_role_permissions", {"role_name": "NonExistent Role"})

        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("not found", tool_output["error"])
        self.assertIn("hint", tool_output)

    def test_check_role_permissions_suggests_similar_roles(self):
        """Positive: check_role_permissions suggests similar role names on partial match."""
        Role.objects.create(name="Patch Reviewer", tenant=self.tenant)
        Role.objects.create(name="Patch Admin", tenant=self.tenant)

        response = self._call_tool("check_role_permissions", {"role_name": "Patch Review"})

        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("did_you_mean", tool_output)
        suggestions = [s["name"] for s in tool_output["did_you_mean"]]
        self.assertIn("Patch Reviewer", suggestions)

    def test_check_role_permissions_case_insensitive(self):
        """Positive: check_role_permissions finds role with case-insensitive search."""
        role = Role.objects.create(name="Patch Reviewer", tenant=self.tenant)
        Access.objects.create(role=role, permission=self.patch_read_perm, tenant=self.tenant)

        response = self._call_tool("check_role_permissions", {"role_name": "patch reviewer"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["role"]["name"], "Patch Reviewer")

    def test_check_role_permissions_empty_role(self):
        """Positive: check_role_permissions warns about roles with no permissions."""
        Role.objects.create(name="Empty Role", tenant=self.tenant)

        response = self._call_tool("check_role_permissions", {"role_name": "Empty Role"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["permissions"]["total_count"], 0)
        self.assertTrue(any("no permissions" in r.lower() for r in tool_output["recommendations"]))

    def test_check_role_permissions_includes_available_permissions(self):
        """Positive: check_role_permissions lists available but not granted permissions."""
        role = Role.objects.create(name="Patch Reader", tenant=self.tenant)
        Access.objects.create(role=role, permission=self.patch_read_perm, tenant=self.tenant)

        response = self._call_tool(
            "check_role_permissions",
            {"role_name": "Patch Reader", "include_available_permissions": True},
        )

        tool_output = self._get_tool_output(response)
        self.assertIn("available_but_not_granted", tool_output)
        self.assertIn("patch", tool_output["available_but_not_granted"])
        patch_not_granted = tool_output["available_but_not_granted"]["patch"]
        self.assertIn("patch:systems:write", patch_not_granted)

    def test_check_role_permissions_without_auth_returns_error(self):
        """Permission: check_role_permissions without auth returns auth error."""
        response = self._call_tool("check_role_permissions", {"role_name": "Any Role"}, use_auth=False)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_check_role_permissions_ignores_system_roles(self):
        """Negative: check_role_permissions only checks custom roles, not system roles from public tenant."""
        public_tenant = Tenant.objects.filter(tenant_name="public").first()
        if not public_tenant:
            public_tenant = Tenant.objects.create(tenant_name="public", org_id="")

        system_role = Role.objects.create(name="System Reader", system=True, tenant=public_tenant)
        system_perm = Permission.objects.create(
            application="system",
            resource_type="*",
            verb="read",
            permission="system:*:read",
            tenant=public_tenant,
        )
        Access.objects.create(role=system_role, permission=system_perm, tenant=public_tenant)

        response = self._call_tool("check_role_permissions", {"role_name": "System Reader"})

        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("not found", tool_output["error"])

    def test_check_role_permissions_returns_org_version_v1(self):
        """Positive: check_role_permissions returns org_version=v1 for V1 orgs."""
        role = Role.objects.create(name="V1 Test Role", tenant=self.tenant)
        Access.objects.create(role=role, permission=self.patch_read_perm, tenant=self.tenant)

        response = self._call_tool("check_role_permissions", {"role_name": "V1 Test Role"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["org_version"], "v1")


@override_settings(BYPASS_BOP_VERIFICATION=True, V2_APIS_ENABLED=True)
class MCPCheckRolePermissionsV2Tests(MCPToolTestMixin, IdentityRequest):
    """Tests for check_role_permissions on V2 organizations."""

    def setUp(self):
        """Set up V2 check_role_permissions tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()

        self.enterContext(
            patch(
                "management.permissions.role_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )

        # Activate V2 for this tenant
        TenantMapping.objects.create(tenant=self.tenant, v2_write_activated_at=timezone.now())

        # Create permissions for testing
        self.patch_read_perm = Permission.objects.create(
            application="patch",
            resource_type="*",
            verb="read",
            permission="patch:*:read",
            tenant=self.tenant,
        )
        self.remediation_read_perm = Permission.objects.create(
            application="remediations",
            resource_type="*",
            verb="read",
            permission="remediations:*:read",
            tenant=self.tenant,
        )
        self.patch_write_perm = Permission.objects.create(
            application="patch",
            resource_type="systems",
            verb="write",
            permission="patch:systems:write",
            tenant=self.tenant,
        )

    def tearDown(self):
        """Tear down V2 check_role_permissions tests."""
        RoleV2.objects.all().delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        TenantMapping.objects.all().delete()
        super().tearDown()

    def test_check_role_permissions_v2_success(self):
        """Positive: check_role_permissions returns comprehensive analysis for V2 roles."""
        role = RoleV2.objects.create(name="V2 Patch Reviewer", tenant=self.tenant, type=RoleV2.Types.CUSTOM)
        role.permissions.add(self.patch_read_perm)
        role.permissions.add(self.remediation_read_perm)

        response = self._call_tool("check_role_permissions", {"role_name": "V2 Patch Reviewer"})

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)

        # Verify role info
        self.assertEqual(tool_output["role"]["name"], "V2 Patch Reviewer")
        self.assertEqual(tool_output["role"]["type"], "custom")
        self.assertFalse(tool_output["role"]["system"])
        self.assertIn("uuid", tool_output["role"])

        # Verify permissions structure
        self.assertEqual(tool_output["permissions"]["total_count"], 2)
        self.assertIn("patch", tool_output["permissions"]["by_application"])
        self.assertIn("remediations", tool_output["permissions"]["by_application"])

        # Verify org_version
        self.assertEqual(tool_output["org_version"], "v2")

    def test_check_role_permissions_v2_returns_org_version(self):
        """Positive: check_role_permissions returns org_version=v2 for V2 orgs."""
        role = RoleV2.objects.create(name="V2 Test Role", tenant=self.tenant, type=RoleV2.Types.CUSTOM)
        role.permissions.add(self.patch_read_perm)

        response = self._call_tool("check_role_permissions", {"role_name": "V2 Test Role"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["org_version"], "v2")

    def test_check_role_permissions_v2_ignores_seeded_roles(self):
        """Negative: check_role_permissions only checks custom roles, not seeded roles from public tenant."""
        public_tenant = Tenant.objects.filter(tenant_name="public").first()
        if not public_tenant:
            public_tenant = Tenant.objects.create(tenant_name="public", org_id="")

        seeded_role = RoleV2.objects.create(name="Seeded Reader", tenant=public_tenant, type=RoleV2.Types.SEEDED)
        seeded_perm = Permission.objects.create(
            application="inventory",
            resource_type="hosts",
            verb="read",
            permission="inventory:hosts:read",
            tenant=public_tenant,
        )
        seeded_role.permissions.add(seeded_perm)

        response = self._call_tool("check_role_permissions", {"role_name": "Seeded Reader"})

        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("not found", tool_output["error"])

    def test_check_role_permissions_v2_identifies_read_only(self):
        """Positive: check_role_permissions correctly identifies read-only V2 roles."""
        role = RoleV2.objects.create(name="V2 Viewer", tenant=self.tenant, type=RoleV2.Types.CUSTOM)
        role.permissions.add(self.patch_read_perm)

        response = self._call_tool("check_role_permissions", {"role_name": "V2 Viewer"})

        tool_output = self._get_tool_output(response)
        self.assertTrue(tool_output["coverage_analysis"]["is_read_only"])
        self.assertFalse(tool_output["coverage_analysis"]["can_modify"])

    def test_check_role_permissions_v2_identifies_write_capability(self):
        """Positive: check_role_permissions correctly identifies V2 roles with write access."""
        role = RoleV2.objects.create(name="V2 Admin", tenant=self.tenant, type=RoleV2.Types.CUSTOM)
        role.permissions.add(self.patch_read_perm)
        role.permissions.add(self.patch_write_perm)

        response = self._call_tool("check_role_permissions", {"role_name": "V2 Admin"})

        tool_output = self._get_tool_output(response)
        self.assertFalse(tool_output["coverage_analysis"]["is_read_only"])
        self.assertTrue(tool_output["coverage_analysis"]["can_modify"])
        self.assertIn("write", tool_output["permissions"]["verbs_included"])

    def test_check_role_permissions_v2_suggests_similar_roles(self):
        """Positive: check_role_permissions suggests similar V2 role names on partial match."""
        RoleV2.objects.create(name="Patch Reviewer V2", tenant=self.tenant, type=RoleV2.Types.CUSTOM)
        RoleV2.objects.create(name="Patch Admin V2", tenant=self.tenant, type=RoleV2.Types.CUSTOM)

        response = self._call_tool("check_role_permissions", {"role_name": "Patch Review"})

        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("did_you_mean", tool_output)
        suggestions = [s["name"] for s in tool_output["did_you_mean"]]
        self.assertIn("Patch Reviewer V2", suggestions)

    def test_check_role_permissions_v2_expands_wildcards(self):
        """Positive: check_role_permissions expands wildcard permissions for V2 roles."""
        role = RoleV2.objects.create(name="V2 Wildcard Role", tenant=self.tenant, type=RoleV2.Types.CUSTOM)
        role.permissions.add(self.patch_read_perm)

        response = self._call_tool("check_role_permissions", {"role_name": "V2 Wildcard Role"})

        tool_output = self._get_tool_output(response)
        self.assertIn("expanded_permissions", tool_output["permissions"])
        expanded = tool_output["permissions"]["expanded_permissions"]
        self.assertTrue(any("all patch resources" in exp for exp in expanded))

    def test_check_role_permissions_v2_role_not_found(self):
        """Negative: check_role_permissions returns error for non-existent V2 role."""
        response = self._call_tool("check_role_permissions", {"role_name": "NonExistent V2 Role"})

        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("not found", tool_output["error"])
        self.assertIn("hint", tool_output)

    def test_check_role_permissions_v2_case_insensitive(self):
        """Positive: check_role_permissions finds V2 role with case-insensitive search."""
        role = RoleV2.objects.create(name="V2 Patch Reviewer", tenant=self.tenant, type=RoleV2.Types.CUSTOM)
        role.permissions.add(self.patch_read_perm)

        response = self._call_tool("check_role_permissions", {"role_name": "v2 patch reviewer"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["role"]["name"], "V2 Patch Reviewer")

    def test_check_role_permissions_v2_empty_role(self):
        """Positive: check_role_permissions warns about V2 roles with no permissions."""
        RoleV2.objects.create(name="V2 Empty Role", tenant=self.tenant, type=RoleV2.Types.CUSTOM)

        response = self._call_tool("check_role_permissions", {"role_name": "V2 Empty Role"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["permissions"]["total_count"], 0)
        self.assertTrue(any("no permissions" in r.lower() for r in tool_output["recommendations"]))

    def test_check_role_permissions_v2_includes_available_permissions(self):
        """Positive: check_role_permissions lists available but not granted permissions for V2."""
        role = RoleV2.objects.create(name="V2 Patch Reader", tenant=self.tenant, type=RoleV2.Types.CUSTOM)
        role.permissions.add(self.patch_read_perm)

        response = self._call_tool(
            "check_role_permissions",
            {"role_name": "V2 Patch Reader", "include_available_permissions": True},
        )

        tool_output = self._get_tool_output(response)
        self.assertIn("available_but_not_granted", tool_output)
        self.assertIn("patch", tool_output["available_but_not_granted"])
        patch_not_granted = tool_output["available_but_not_granted"]["patch"]
        self.assertIn("patch:systems:write", patch_not_granted)

    def test_check_role_permissions_v2_without_auth_returns_error(self):
        """Permission: check_role_permissions without auth returns auth error for V2."""
        response = self._call_tool("check_role_permissions", {"role_name": "Any V2 Role"}, use_auth=False)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)


class MCPCheckRolePermissionsEdgeCasesTests(MCPToolTestMixin, IdentityRequest):
    """Edge case tests for check_role_permissions (V1 - shared logic applies to V2)."""

    def setUp(self):
        """Set up edge case tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()

    def tearDown(self):
        """Tear down edge case tests."""
        Access.objects.all().delete()
        Role.objects.all().delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    def test_check_role_permissions_full_wildcard_warning(self):
        """Positive: check_role_permissions warns about full wildcard (*:*) permissions."""
        full_wildcard_perm = Permission.objects.create(
            application="dangerous",
            resource_type="*",
            verb="*",
            permission="dangerous:*:*",
            tenant=self.tenant,
        )
        role = Role.objects.create(name="Full Access Role", tenant=self.tenant)
        Access.objects.create(role=role, permission=full_wildcard_perm, tenant=self.tenant)

        response = self._call_tool("check_role_permissions", {"role_name": "Full Access Role"})

        tool_output = self._get_tool_output(response)
        self.assertTrue(tool_output["coverage_analysis"]["has_wildcard_resource"])
        self.assertTrue(tool_output["coverage_analysis"]["has_wildcard_verb"])
        self.assertTrue(any("CAUTION" in r for r in tool_output["recommendations"]))
        self.assertTrue(any("full access" in r.lower() for r in tool_output["recommendations"]))

    def test_check_role_permissions_multi_app_warning(self):
        """Positive: check_role_permissions warns about roles spanning many applications."""
        # Create permissions for 4 different applications
        apps = ["app1", "app2", "app3", "app4"]
        perms = []
        for app in apps:
            perm = Permission.objects.create(
                application=app,
                resource_type="resource",
                verb="read",
                permission=f"{app}:resource:read",
                tenant=self.tenant,
            )
            perms.append(perm)

        role = Role.objects.create(name="Multi App Role", tenant=self.tenant)
        for perm in perms:
            Access.objects.create(role=role, permission=perm, tenant=self.tenant)

        response = self._call_tool("check_role_permissions", {"role_name": "Multi App Role"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["coverage_analysis"]["applications_covered"]), 4)
        self.assertTrue(any("spans 4 applications" in r for r in tool_output["recommendations"]))

    def test_check_role_permissions_verbs_not_included(self):
        """Positive: check_role_permissions lists verbs not included in recommendations."""
        read_only_perm = Permission.objects.create(
            application="test",
            resource_type="resource",
            verb="read",
            permission="test:resource:read",
            tenant=self.tenant,
        )
        role = Role.objects.create(name="Read Only Role", tenant=self.tenant)
        Access.objects.create(role=role, permission=read_only_perm, tenant=self.tenant)

        response = self._call_tool("check_role_permissions", {"role_name": "Read Only Role"})

        tool_output = self._get_tool_output(response)
        verbs_not_included = tool_output["permissions"]["verbs_not_included"]
        self.assertIn("write", verbs_not_included)
        self.assertIn("create", verbs_not_included)
        self.assertIn("delete", verbs_not_included)
        self.assertTrue(any("Verbs NOT granted" in r for r in tool_output["recommendations"]))

    def test_check_role_permissions_wildcard_verb_clears_verbs_not_included(self):
        """Positive: Wildcard verb (*) results in empty verbs_not_included list."""
        wildcard_verb_perm = Permission.objects.create(
            application="test",
            resource_type="resource",
            verb="*",
            permission="test:resource:*",
            tenant=self.tenant,
        )
        role = Role.objects.create(name="Wildcard Verb Role", tenant=self.tenant)
        Access.objects.create(role=role, permission=wildcard_verb_perm, tenant=self.tenant)

        response = self._call_tool("check_role_permissions", {"role_name": "Wildcard Verb Role"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["permissions"]["verbs_not_included"], [])
        self.assertTrue(tool_output["coverage_analysis"]["has_wildcard_verb"])
        self.assertTrue(tool_output["coverage_analysis"]["can_modify"])

    def test_check_role_permissions_multiple_verbs(self):
        """Positive: check_role_permissions correctly tracks multiple verbs."""
        read_perm = Permission.objects.create(
            application="test",
            resource_type="resource",
            verb="read",
            permission="test:resource:read",
            tenant=self.tenant,
        )
        write_perm = Permission.objects.create(
            application="test",
            resource_type="resource",
            verb="write",
            permission="test:resource:write",
            tenant=self.tenant,
        )
        create_perm = Permission.objects.create(
            application="test",
            resource_type="resource",
            verb="create",
            permission="test:resource:create",
            tenant=self.tenant,
        )
        role = Role.objects.create(name="Multi Verb Role", tenant=self.tenant)
        Access.objects.create(role=role, permission=read_perm, tenant=self.tenant)
        Access.objects.create(role=role, permission=write_perm, tenant=self.tenant)
        Access.objects.create(role=role, permission=create_perm, tenant=self.tenant)

        response = self._call_tool("check_role_permissions", {"role_name": "Multi Verb Role"})

        tool_output = self._get_tool_output(response)
        verbs_included = tool_output["permissions"]["verbs_included"]
        self.assertIn("read", verbs_included)
        self.assertIn("write", verbs_included)
        self.assertIn("create", verbs_included)
        self.assertNotIn("delete", verbs_included)
        self.assertIn("delete", tool_output["permissions"]["verbs_not_included"])
        self.assertFalse(tool_output["coverage_analysis"]["is_read_only"])
        self.assertTrue(tool_output["coverage_analysis"]["can_modify"])


@override_settings(BYPASS_BOP_VERIFICATION=True)
class AuditGroupForDissolutionTests(MCPToolTestMixin, IdentityRequest):
    """Tests for audit_group_for_dissolution MCP tool."""

    def setUp(self):
        """Set up audit_group_for_dissolution tests with groups, roles, and members."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()

        # Create test principals
        self.user1 = Principal.objects.create(username="jen.park", tenant=self.tenant, type="user")
        self.user2 = Principal.objects.create(username="tomas.rivera", tenant=self.tenant, type="user")
        self.user3 = Principal.objects.create(username="priya.shah", tenant=self.tenant, type="user")
        self.user4 = Principal.objects.create(username="alex.chen", tenant=self.tenant, type="user")
        self.svc1 = Principal.objects.create(
            username="svc-legacy-patcher",
            tenant=self.tenant,
            type="service-account",
            service_account_id="sa-12345",
        )
        self.svc2 = Principal.objects.create(
            username="svc-legacy-remediation",
            tenant=self.tenant,
            type="service-account",
            service_account_id="sa-67890",
        )

        # Create target group to be dissolved
        self.legacy_ops_group = Group.objects.create(
            name="Legacy Ops",
            description="Legacy operations team",
            tenant=self.tenant,
        )

        # Create platform default group
        self.platform_default_group = Group.objects.create(
            name="Default access",
            description="Platform default group",
            tenant=self.tenant,
            platform_default=True,
        )

        # Create another group for users with overlapping access
        self.other_group = Group.objects.create(
            name="Engineering",
            description="Engineering team",
            tenant=self.tenant,
        )

        # Add members to Legacy Ops
        self.legacy_ops_group.principals.add(self.user1, self.user2, self.user3, self.user4, self.svc1, self.svc2)

        # Add some users to platform default (they'll be stranded after dissolution)
        self.platform_default_group.principals.add(self.user1, self.user2, self.user3)

        # Add user4 to another group (will have overlapping access)
        self.other_group.principals.add(self.user4)

        # Create roles with permissions
        self.inventory_role = Role.objects.create(
            name="inventory-hosts-admin",
            display_name="Inventory Hosts administrator",
            description="Manage inventory hosts",
            tenant=self.tenant,
        )
        self.patch_role = Role.objects.create(
            name="patch-admin",
            display_name="Patch administrator",
            description="Manage patches",
            tenant=self.tenant,
        )
        self.remediation_role = Role.objects.create(
            name="remediations-admin",
            display_name="Remediations administrator",
            description="Manage remediations",
            tenant=self.tenant,
        )

        # Create permissions
        self.inventory_perm = Permission.objects.create(
            permission="inventory:hosts:write",
            tenant=self.tenant,
        )
        self.patch_perm = Permission.objects.create(
            permission="patch:template:write",
            tenant=self.tenant,
        )
        self.remediation_perm = Permission.objects.create(
            permission="remediations:remediation:write",
            tenant=self.tenant,
        )

        # Assign permissions to roles
        Access.objects.create(permission=self.inventory_perm, role=self.inventory_role, tenant=self.tenant)
        Access.objects.create(permission=self.patch_perm, role=self.patch_role, tenant=self.tenant)
        Access.objects.create(permission=self.remediation_perm, role=self.remediation_role, tenant=self.tenant)

        # Create policy and add roles to Legacy Ops group
        self.policy = Policy.objects.create(name="legacy-ops-policy", group=self.legacy_ops_group, tenant=self.tenant)
        self.policy.roles.add(self.inventory_role, self.patch_role, self.remediation_role)

        # Add a role to the other group for overlapping access
        other_policy = Policy.objects.create(name="engineering-policy", group=self.other_group, tenant=self.tenant)
        other_policy.roles.add(self.inventory_role)

    def tearDown(self):
        """Clean up test data."""
        Policy.objects.filter(tenant=self.tenant).delete()
        Access.objects.filter(tenant=self.tenant).delete()
        Role.objects.filter(tenant=self.tenant).delete()
        Permission.objects.filter(tenant=self.tenant).delete()
        Group.objects.filter(tenant=self.tenant).delete()
        Principal.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    def test_audit_group_for_dissolution_success(self):
        """Positive: audit_group_for_dissolution returns group dissolution analysis."""
        response = self._call_tool("audit_group_for_dissolution", {"group_name": "Legacy Ops"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)

        self.assertIn("group", tool_output)
        self.assertIn("members", tool_output)
        self.assertIn("roles", tool_output)
        self.assertIn("analysis", tool_output)
        self.assertEqual(tool_output["group"]["name"], "Legacy Ops")

    def test_audit_group_for_dissolution_without_auth_returns_error(self):
        """Permission: audit_group_for_dissolution without auth returns auth error."""
        response = self._call_tool("audit_group_for_dissolution", {"group_name": "Legacy Ops"}, use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_audit_group_for_dissolution_identifies_stranded_users(self):
        """Positive: identifies users who would be stranded (only in target + platform default)."""
        response = self._call_tool("audit_group_for_dissolution", {"group_name": "Legacy Ops"})
        tool_output = self._get_tool_output(response)

        analysis = tool_output["analysis"]
        # Users 1, 2, 3 are only in Legacy Ops + platform default - they're stranded (identified by UUID)
        self.assertEqual(analysis["stranded_user_count"], 3)
        self.assertIn(str(self.user1.uuid), analysis["stranded_users"])
        self.assertIn(str(self.user2.uuid), analysis["stranded_users"])
        self.assertIn(str(self.user3.uuid), analysis["stranded_users"])

    def test_audit_group_for_dissolution_identifies_stranded_service_accounts(self):
        """Positive: identifies service accounts that would lose all access."""
        response = self._call_tool("audit_group_for_dissolution", {"group_name": "Legacy Ops"})
        tool_output = self._get_tool_output(response)

        analysis = tool_output["analysis"]
        # Both service accounts are only in Legacy Ops - they're stranded (identified by UUID)
        self.assertEqual(analysis["stranded_service_account_count"], 2)
        self.assertIn(str(self.svc1.uuid), analysis["stranded_service_accounts"])
        self.assertIn(str(self.svc2.uuid), analysis["stranded_service_accounts"])

    def test_audit_group_for_dissolution_identifies_members_with_overlapping_access(self):
        """Positive: identifies members who have overlapping access via other groups."""
        response = self._call_tool("audit_group_for_dissolution", {"group_name": "Legacy Ops"})
        tool_output = self._get_tool_output(response)

        analysis = tool_output["analysis"]
        # User4 (alex.chen) is also in Engineering group — identified by UUID
        self.assertIn(str(self.user4.uuid), analysis["members_with_overlapping_access"])
        self.assertEqual(analysis["members_with_overlapping_count"], 1)

    def test_audit_group_for_dissolution_shows_roles_and_permissions(self):
        """Positive: returns roles assigned to the group with their permissions."""
        response = self._call_tool("audit_group_for_dissolution", {"group_name": "Legacy Ops"})
        tool_output = self._get_tool_output(response)

        roles = tool_output["roles"]
        self.assertEqual(len(roles), 3)
        role_names = [r["name"] for r in roles]
        self.assertIn("Inventory Hosts administrator", role_names)
        self.assertIn("Patch administrator", role_names)
        self.assertIn("Remediations administrator", role_names)

        analysis = tool_output["analysis"]
        self.assertEqual(analysis["total_unique_permissions"], 3)
        self.assertIn("inventory", analysis["permissions_by_application"])
        self.assertIn("patch", analysis["permissions_by_application"])
        self.assertIn("remediations", analysis["permissions_by_application"])

    def test_audit_group_for_dissolution_member_details(self):
        """Positive: returns detailed member information including other groups."""
        response = self._call_tool("audit_group_for_dissolution", {"group_name": "Legacy Ops"})
        tool_output = self._get_tool_output(response)

        members = tool_output["members"]
        self.assertEqual(len(members), 6)

        # Find alex.chen by UUID (PII redacted from responses)
        alex = next(m for m in members if m["uuid"] == str(self.user4.uuid))
        self.assertNotIn("username", alex)
        self.assertFalse(alex["is_stranded"])
        self.assertEqual(alex["non_default_group_count"], 1)
        self.assertIn("partial", alex["access_impact"])

        # Find svc-legacy-patcher (service account) by UUID
        svc = next(m for m in members if m["uuid"] == str(self.svc1.uuid))
        self.assertTrue(svc["is_stranded"])
        self.assertEqual(svc["type"], "service-account")
        self.assertEqual(svc["service_account_id"], "sa-12345")

    def test_audit_group_for_dissolution_by_uuid(self):
        """Positive: can look up group by UUID instead of name."""
        response = self._call_tool("audit_group_for_dissolution", {"group_uuid": str(self.legacy_ops_group.uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["group"]["name"], "Legacy Ops")

    def test_audit_group_for_dissolution_group_not_found(self):
        """Negative: returns error for non-existent group."""
        response = self._call_tool("audit_group_for_dissolution", {"group_name": "Non-Existent Group"})

        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output)
        self.assertIn("not found", tool_output["error"])

    def test_audit_group_for_dissolution_empty_group(self):
        """Edge case: handles group with no members."""
        empty_group = Group.objects.create(name="Empty Group", tenant=self.tenant)

        response = self._call_tool("audit_group_for_dissolution", {"group_name": "Empty Group"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["members"]), 0)
        self.assertEqual(tool_output["analysis"]["total_members"], 0)
        self.assertIn("no members", tool_output["analysis"]["warnings"][0].lower())

        empty_group.delete()

    def test_audit_group_for_dissolution_warns_about_stranded(self):
        """Positive: warnings field includes stranded user/service account alerts."""
        response = self._call_tool("audit_group_for_dissolution", {"group_name": "Legacy Ops"})
        tool_output = self._get_tool_output(response)

        warnings = tool_output["analysis"]["warnings"]
        self.assertTrue(len(warnings) >= 2)

        # Check for user warning
        user_warning = next(w for w in warnings if "user" in w.lower() and "demoted" in w.lower())
        self.assertIn("3", user_warning)

        # Check for service account warning
        svc_warning = next(w for w in warnings if "service account" in w.lower())
        self.assertIn("403", svc_warning)

    @override_settings(MCP_PII_REDACTION_ENABLED=False)
    def test_audit_group_for_dissolution_pii_visible_when_redaction_disabled(self):
        """Positive: audit_group_for_dissolution returns usernames when redaction is disabled via env var."""
        response = self._call_tool("audit_group_for_dissolution", {"group_name": "Legacy Ops"})
        tool_output = self._get_tool_output(response)

        members = tool_output["members"]
        alex = next(m for m in members if m["uuid"] == str(self.user4.uuid))
        self.assertIn("username", alex)
        self.assertEqual(alex["username"], "alex.chen")

        analysis = tool_output["analysis"]
        self.assertIn("jen.park", analysis["stranded_users"])


@override_settings(BYPASS_BOP_VERIFICATION=True)
class MCPInvestigateUserAccessTests(MCPToolTestMixin, IdentityRequest):
    """Tests for investigate_user_access MCP tool."""

    def setUp(self):
        """Set up investigate_user_access tests with groups, roles, and permissions."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.test_username = "sarah"
        self.principal = Principal.objects.create(username=self.test_username, tenant=self.tenant)

        # Create two groups - "Compliance Auditors" and "Compliance Admins"
        self.auditors_group = Group.objects.create(
            name="Compliance Auditors",
            description="Read-only access to compliance data",
            tenant=self.tenant,
        )
        self.admins_group = Group.objects.create(
            name="Compliance Admins",
            description="Administrative access to compliance",
            tenant=self.tenant,
        )

        # Add principal to both groups
        self.auditors_group.principals.add(self.principal)
        self.admins_group.principals.add(self.principal)

        # Create roles
        self.auditor_role = Role.objects.create(
            name="Compliance Auditor",
            display_name="Compliance Auditor",
            description="Read-only compliance access",
            tenant=self.tenant,
        )
        self.admin_role = Role.objects.create(
            name="Compliance administrator",
            display_name="Compliance administrator",
            description="Compliance administration (misleading - only has read)",
            tenant=self.tenant,
        )

        # Create permissions
        self.read_perm = Permission.objects.create(
            application="compliance",
            resource_type="policies",
            verb="read",
            permission="compliance:policies:read",
            tenant=self.tenant,
        )
        self.write_perm = Permission.objects.create(
            application="compliance",
            resource_type="policies",
            verb="write",
            permission="compliance:policies:write",
            tenant=self.tenant,
        )

        # Auditor role only has read permission
        Access.objects.create(permission=self.read_perm, role=self.auditor_role, tenant=self.tenant)

        # Admin role ALSO only has read permission (this is the "gotcha")
        Access.objects.create(permission=self.read_perm, role=self.admin_role, tenant=self.tenant)

        # Assign roles to groups via policies
        auditors_policy = Policy.objects.create(name="auditors_policy", group=self.auditors_group, tenant=self.tenant)
        auditors_policy.roles.add(self.auditor_role)

        admins_policy = Policy.objects.create(name="admins_policy", group=self.admins_group, tenant=self.tenant)
        admins_policy.roles.add(self.admin_role)

    def tearDown(self):
        """Tear down investigate_user_access tests."""
        Access.objects.all().delete()
        Policy.objects.all().delete()
        Role.objects.all().delete()
        Permission.objects.all().delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    def test_investigate_user_access_success(self):
        """Positive: investigate_user_access returns comprehensive user access info."""
        response = self._call_tool("investigate_user_access", {"username": self.test_username})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)

        # Verify structure
        self.assertIn("user", tool_output)
        self.assertIn("groups", tool_output)
        self.assertIn("analysis", tool_output)
        self.assertIn("permission_sources", tool_output)
        self.assertIn("hints", tool_output)

        # Verify user info
        self.assertEqual(tool_output["user"]["username"], self.test_username)
        self.assertTrue(tool_output["user"]["exists"])

    def test_investigate_user_access_without_auth_returns_error(self):
        """Permission: investigate_user_access without auth returns auth error."""
        response = self._call_tool("investigate_user_access", {"username": self.test_username}, use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_investigate_user_access_user_not_found(self):
        """Negative: investigate_user_access returns error for non-existent user."""
        response = self._call_tool("investigate_user_access", {"username": "nonexistent_user"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)

        self.assertIn("error", tool_output)
        self.assertIn("not found", tool_output["error"])
        self.assertIn("hint", tool_output)

    @patch("management.mcp_views._list_principals_by_name")
    def test_investigate_user_access_resolves_display_name(self, mock_by_name):
        """Positive: investigate_user_access auto-resolves a display name to a username."""
        mock_by_name.return_value = json.dumps(
            {
                "data": [
                    {"username": self.test_username, "first_name": "Sarah", "last_name": "Connor"},
                ]
            }
        )
        response = self._call_tool("investigate_user_access", {"username": "Sarah"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)

        self.assertNotIn("error", tool_output)
        self.assertEqual(tool_output["user"]["username"], self.test_username)

    @patch("management.mcp_views._list_principals_by_name")
    def test_investigate_user_access_multiple_name_matches(self, mock_by_name):
        """Negative: investigate_user_access returns candidates when multiple users match a display name."""
        mock_by_name.return_value = json.dumps(
            {
                "data": [
                    {"username": "sarah1", "first_name": "Sarah", "last_name": "Connor"},
                    {"username": "sarah2", "first_name": "Sarah", "last_name": "Smith"},
                ]
            }
        )
        response = self._call_tool("investigate_user_access", {"username": "Sarah"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)

        self.assertIn("error", tool_output)
        self.assertIn("Multiple users match", tool_output["error"])
        self.assertEqual(len(tool_output["candidates"]), 2)
        usernames = [c["username"] for c in tool_output["candidates"]]
        self.assertIn("sarah1", usernames)
        self.assertIn("sarah2", usernames)

    def test_investigate_user_access_lists_all_groups(self):
        """Positive: investigate_user_access lists all groups the user belongs to."""
        response = self._call_tool("investigate_user_access", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["analysis"]["total_groups"], 2)
        self.assertEqual(len(tool_output["groups"]), 2)

        group_names = [g["name"] for g in tool_output["groups"]]
        self.assertIn("Compliance Auditors", group_names)
        self.assertIn("Compliance Admins", group_names)

    def test_investigate_user_access_lists_roles_per_group(self):
        """Positive: investigate_user_access lists roles assigned to each group."""
        response = self._call_tool("investigate_user_access", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        for group in tool_output["groups"]:
            self.assertIn("roles", group)
            self.assertEqual(group["role_count"], 1)
            self.assertEqual(len(group["roles"]), 1)

            if group["name"] == "Compliance Auditors":
                self.assertEqual(group["roles"][0]["display_name"], "Compliance Auditor")
            elif group["name"] == "Compliance Admins":
                self.assertEqual(group["roles"][0]["display_name"], "Compliance administrator")

    def test_investigate_user_access_expands_role_permissions(self):
        """Positive: investigate_user_access expands roles to show actual permissions."""
        response = self._call_tool("investigate_user_access", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        for group in tool_output["groups"]:
            for role in group["roles"]:
                self.assertIn("permissions", role)
                self.assertIn("permission_count", role)
                self.assertGreaterEqual(role["permission_count"], 1)
                self.assertIn("compliance:policies:read", role["permissions"])

    def test_investigate_user_access_tracks_permission_sources(self):
        """Positive: investigate_user_access tracks which groups/roles grant each permission."""
        response = self._call_tool("investigate_user_access", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        # compliance:policies:read should come from both groups/roles
        self.assertIn("compliance:policies:read", tool_output["permission_sources"])
        sources = tool_output["permission_sources"]["compliance:policies:read"]
        self.assertEqual(len(sources), 2)

        source_groups = [s["group"] for s in sources]
        self.assertIn("Compliance Auditors", source_groups)
        self.assertIn("Compliance Admins", source_groups)

    def test_investigate_user_access_filter_by_application(self):
        """Positive: investigate_user_access filters by application."""
        response = self._call_tool(
            "investigate_user_access",
            {"username": self.test_username, "application": "compliance"},
        )

        tool_output = self._get_tool_output(response)

        self.assertIn("permissions_for_application", tool_output["analysis"])
        self.assertEqual(tool_output["analysis"]["application_permission_count"], 1)
        self.assertIn("compliance:policies:read", tool_output["analysis"]["permissions_for_application"])

    def test_investigate_user_access_expected_permission_found(self):
        """Positive: investigate_user_access identifies when expected permission is present."""
        response = self._call_tool(
            "investigate_user_access",
            {
                "username": self.test_username,
                "application": "compliance",
                "expected_permission": "read",
            },
        )

        tool_output = self._get_tool_output(response)

        self.assertTrue(tool_output["analysis"]["has_expected_permission"])
        check = tool_output["analysis"]["expected_permission_check"]
        self.assertTrue(check["found"])
        self.assertEqual(check["matched_permission"], "compliance:policies:read")
        self.assertIn("granted_via", check)

    def test_investigate_user_access_expected_permission_missing(self):
        """Positive: investigate_user_access identifies when expected permission is missing."""
        response = self._call_tool(
            "investigate_user_access",
            {
                "username": self.test_username,
                "application": "compliance",
                "expected_permission": "write",
            },
        )

        tool_output = self._get_tool_output(response)

        self.assertFalse(tool_output["analysis"]["has_expected_permission"])
        check = tool_output["analysis"]["expected_permission_check"]
        self.assertFalse(check["found"])
        self.assertIn("available_verbs_for_app", check)
        self.assertIn("read", check["available_verbs_for_app"])
        self.assertNotIn("write", check["available_verbs_for_app"])

    def test_investigate_user_access_identifies_gaps(self):
        """Positive: investigate_user_access explains why expected permission is missing."""
        response = self._call_tool(
            "investigate_user_access",
            {
                "username": self.test_username,
                "application": "compliance",
                "expected_permission": "write",
            },
        )

        tool_output = self._get_tool_output(response)

        self.assertIn("gaps", tool_output["analysis"])
        self.assertIn("diagnosis", tool_output["analysis"])
        self.assertGreater(len(tool_output["analysis"]["gaps"]), 0)

        # Should mention that the groups don't grant write
        gaps_text = " ".join(tool_output["analysis"]["gaps"])
        self.assertIn("NOT", gaps_text)
        self.assertIn("write", gaps_text)

    def test_investigate_user_access_expected_verb_parameter(self):
        """Positive: investigate_user_access accepts expected_verb as alternative."""
        response = self._call_tool(
            "investigate_user_access",
            {
                "username": self.test_username,
                "application": "compliance",
                "expected_verb": "read",
            },
        )

        tool_output = self._get_tool_output(response)
        self.assertTrue(tool_output["analysis"]["has_expected_permission"])

    def test_investigate_user_access_user_with_no_groups(self):
        """Positive: investigate_user_access handles user with no groups."""
        # Create a user with no group memberships
        lonely_user = Principal.objects.create(username="lonely_user", tenant=self.tenant)

        response = self._call_tool("investigate_user_access", {"username": "lonely_user"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(len(tool_output["groups"]), 0)
        self.assertIn("not a member of any groups", tool_output["analysis"]["message"])

        # Cleanup
        lonely_user.delete()

    def test_investigate_user_access_includes_hints(self):
        """Positive: investigate_user_access includes helpful hints."""
        response = self._call_tool("investigate_user_access", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        self.assertIn("hints", tool_output)
        self.assertIn("verify_specific_permission", tool_output["hints"])
        self.assertIn("find_role_with_permission", tool_output["hints"])
        self.assertIn("check_role_contents", tool_output["hints"])
        self.assertIn("add_user_to_group", tool_output["hints"])

    def test_investigate_user_access_analysis_summary(self):
        """Positive: investigate_user_access returns summary statistics."""
        response = self._call_tool("investigate_user_access", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        analysis = tool_output["analysis"]
        self.assertEqual(analysis["total_groups"], 2)
        self.assertEqual(analysis["total_roles"], 2)
        self.assertEqual(analysis["total_unique_permissions"], 1)  # Only read, not write

    @patch(
        "management.mcp_views.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": [{"is_org_admin": True}]},
    )
    def test_investigate_user_access_org_admin_bypass(self, mock_proxy):
        """Positive: investigate_user_access notes org admin has implicit access."""
        response = self._call_tool(
            "investigate_user_access",
            {
                "username": self.test_username,
                "application": "compliance",
                "expected_permission": "write",
            },
        )

        tool_output = self._get_tool_output(response)

        # Org admin should bypass permission checks
        self.assertTrue(tool_output["user"]["is_org_admin"])
        self.assertIn("note", tool_output["user"])
        self.assertIn("bypasses", tool_output["user"]["note"])

        # Should still report permission as available due to org admin
        self.assertTrue(tool_output["analysis"]["has_expected_permission"])

    def test_investigate_user_access_scenario_conflicting_groups(self):
        """Integration: Full scenario test for conflicting group access."""
        # This tests the exact scenario from the requirements:
        # "Sarah is in 'Compliance Auditors' AND 'Compliance Admins' but can't edit compliance policies"

        response = self._call_tool(
            "investigate_user_access",
            {
                "username": "sarah",
                "application": "compliance",
                "expected_permission": "write",
            },
        )

        tool_output = self._get_tool_output(response)

        # Verify user is in both groups
        self.assertEqual(tool_output["analysis"]["total_groups"], 2)
        group_names = [g["name"] for g in tool_output["groups"]]
        self.assertIn("Compliance Auditors", group_names)
        self.assertIn("Compliance Admins", group_names)

        # Verify write permission is NOT found
        self.assertFalse(tool_output["analysis"]["has_expected_permission"])

        # Verify the diagnosis explains the issue
        self.assertIn("diagnosis", tool_output["analysis"])
        self.assertIn("does not have", tool_output["analysis"]["diagnosis"])

        # Verify gaps identify both groups lack write
        gaps = tool_output["analysis"]["gaps"]
        self.assertEqual(len(gaps), 2)  # Both groups should be mentioned

        # Verify the "misleading name" issue is discoverable:
        # The Compliance Admins group has "Compliance administrator" role
        # but that role only has read, not write
        admins_group = next(g for g in tool_output["groups"] if g["name"] == "Compliance Admins")
        admin_role = admins_group["roles"][0]
        self.assertEqual(admin_role["display_name"], "Compliance administrator")
        self.assertIn("compliance:policies:read", admin_role["permissions"])
        self.assertNotIn("compliance:policies:write", admin_role["permissions"])

    def test_investigate_user_access_full_permission_format(self):
        """Positive: investigate_user_access handles full permission format."""
        response = self._call_tool(
            "investigate_user_access",
            {
                "username": self.test_username,
                "application": "compliance",
                "expected_permission": "compliance:policies:read",
            },
        )

        tool_output = self._get_tool_output(response)
        self.assertTrue(tool_output["analysis"]["has_expected_permission"])

    def test_investigate_user_access_multiple_permissions_per_role(self):
        """Positive: investigate_user_access handles roles with multiple permissions."""
        # Add another permission to the admin role
        delete_perm = Permission.objects.create(
            application="compliance",
            resource_type="policies",
            verb="delete",
            permission="compliance:policies:delete",
            tenant=self.tenant,
        )
        Access.objects.create(permission=delete_perm, role=self.admin_role, tenant=self.tenant)

        response = self._call_tool(
            "investigate_user_access",
            {"username": self.test_username, "application": "compliance"},
        )

        tool_output = self._get_tool_output(response)

        # Should now have 2 unique permissions
        self.assertEqual(tool_output["analysis"]["application_permission_count"], 2)
        self.assertIn("compliance:policies:read", tool_output["analysis"]["permissions_for_application"])
        self.assertIn("compliance:policies:delete", tool_output["analysis"]["permissions_for_application"])


@override_settings(BYPASS_BOP_VERIFICATION=True, V2_APIS_ENABLED=True)
class MCPInvestigateUserAccessV2Tests(MCPToolTestMixin, IdentityRequest):
    """Tests for investigate_user_access on V2 organizations."""

    def setUp(self):
        """Set up V2 investigate_user_access tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.test_username = "sarah_v2"
        self.principal = Principal.objects.create(username=self.test_username, tenant=self.tenant)

        self.enterContext(
            patch(
                "management.permissions.role_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )

        # Activate V2 for this tenant
        TenantMapping.objects.create(tenant=self.tenant, v2_write_activated_at=timezone.now())

        # Create groups
        self.auditors_group = Group.objects.create(
            name="V2 Compliance Auditors",
            description="Read-only compliance access",
            tenant=self.tenant,
        )
        self.admins_group = Group.objects.create(
            name="V2 Compliance Admins",
            description="Admin compliance access",
            tenant=self.tenant,
        )
        self.auditors_group.principals.add(self.principal)
        self.admins_group.principals.add(self.principal)

        # Create V2 roles with permissions
        self.read_perm = Permission.objects.create(
            application="compliance",
            resource_type="policies",
            verb="read",
            permission="compliance:policies:read",
            tenant=self.tenant,
        )

        self.auditor_role = RoleV2.objects.create(
            name="V2 Compliance Auditor",
            tenant=self.tenant,
        )
        self.auditor_role.permissions.add(self.read_perm)

        self.admin_role = RoleV2.objects.create(
            name="V2 Compliance Administrator",
            tenant=self.tenant,
        )
        self.admin_role.permissions.add(self.read_perm)

        # Create role bindings
        self.auditor_binding = RoleBinding.objects.create(
            role=self.auditor_role,
            resource_type="workspace",
            resource_id="default",
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(binding=self.auditor_binding, group=self.auditors_group)

        self.admin_binding = RoleBinding.objects.create(
            role=self.admin_role,
            resource_type="workspace",
            resource_id="default",
            tenant=self.tenant,
        )
        RoleBindingGroup.objects.create(binding=self.admin_binding, group=self.admins_group)

    def tearDown(self):
        """Tear down V2 investigate_user_access tests."""
        RoleBindingGroup.objects.all().delete()
        RoleBindingPrincipal.objects.all().delete()
        RoleBinding.objects.all().delete()
        RoleV2.objects.all().delete()
        Permission.objects.all().delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()
        TenantMapping.objects.filter(tenant=self.tenant).delete()
        reload(urls)
        clear_url_caches()
        super().tearDown()

    def test_investigate_user_access_v2_returns_org_version(self):
        """Positive: investigate_user_access returns org_version='v2' for V2 orgs."""
        response = self._call_tool("investigate_user_access", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["org_version"], "v2")

    def test_investigate_user_access_v2_includes_role_bindings(self):
        """Positive: investigate_user_access V2 includes role_bindings instead of group.roles."""
        response = self._call_tool("investigate_user_access", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        self.assertIn("role_bindings", tool_output)
        self.assertEqual(len(tool_output["role_bindings"]), 2)

        # Check binding structure
        binding = tool_output["role_bindings"][0]
        self.assertIn("role", binding)
        self.assertIn("permissions", binding["role"])
        self.assertIn("binding_source", binding)
        self.assertIn("resource_type", binding)

    def test_investigate_user_access_v2_shows_binding_source(self):
        """Positive: investigate_user_access V2 shows whether binding is via group or direct."""
        response = self._call_tool("investigate_user_access", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        # All our bindings are via groups
        for binding in tool_output["role_bindings"]:
            self.assertEqual(binding["binding_source"], "group")
            self.assertIsNotNone(binding["source_group"])

    def test_investigate_user_access_v2_expected_permission_found(self):
        """Positive: investigate_user_access V2 finds expected permission."""
        response = self._call_tool(
            "investigate_user_access",
            {
                "username": self.test_username,
                "application": "compliance",
                "expected_permission": "read",
            },
        )

        tool_output = self._get_tool_output(response)
        self.assertTrue(tool_output["analysis"]["has_expected_permission"])

    def test_investigate_user_access_v2_expected_permission_missing(self):
        """Positive: investigate_user_access V2 identifies missing permission."""
        response = self._call_tool(
            "investigate_user_access",
            {
                "username": self.test_username,
                "application": "compliance",
                "expected_permission": "write",
            },
        )

        tool_output = self._get_tool_output(response)
        self.assertFalse(tool_output["analysis"]["has_expected_permission"])
        self.assertIn("gaps", tool_output["analysis"])
        self.assertIn("diagnosis", tool_output["analysis"])

    def test_investigate_user_access_v2_direct_binding(self):
        """Positive: investigate_user_access V2 handles direct principal bindings."""
        # Create a direct binding to the principal
        direct_role = RoleV2.objects.create(name="Direct Role", tenant=self.tenant)
        write_perm, _ = Permission.objects.get_or_create(
            permission="compliance:policies:write",
            defaults={"tenant": self.tenant},
        )
        direct_role.permissions.add(write_perm)

        direct_binding = RoleBinding.objects.create(
            role=direct_role,
            resource_type="workspace",
            resource_id="default",
            tenant=self.tenant,
        )
        RoleBindingPrincipal.objects.create(binding=direct_binding, principal=self.principal, source="system")

        response = self._call_tool(
            "investigate_user_access",
            {
                "username": self.test_username,
                "application": "compliance",
                "expected_permission": "write",
            },
        )

        tool_output = self._get_tool_output(response)

        # Should now find write permission via direct binding
        self.assertTrue(tool_output["analysis"]["has_expected_permission"])

        # Should have a direct binding
        direct_bindings = [b for b in tool_output["role_bindings"] if b["binding_source"] == "direct"]
        self.assertEqual(len(direct_bindings), 1)

    def test_investigate_user_access_v2_includes_resource_scope(self):
        """Positive: investigate_user_access V2 includes resource scope in bindings."""
        response = self._call_tool("investigate_user_access", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        for binding in tool_output["role_bindings"]:
            self.assertIn("resource_type", binding)
            self.assertIn("resource_id", binding)
            self.assertEqual(binding["resource_type"], "workspace")
            self.assertEqual(binding["resource_id"], "default")

    def test_investigate_user_access_v2_permission_sources_include_scope(self):
        """Positive: investigate_user_access V2 permission sources include resource scope."""
        response = self._call_tool("investigate_user_access", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        self.assertIn("compliance:policies:read", tool_output["permission_sources"])
        sources = tool_output["permission_sources"]["compliance:policies:read"]
        self.assertGreater(len(sources), 0)

        for source in sources:
            self.assertIn("resource_scope", source)

    def test_investigate_user_access_v2_hints_include_role_bindings(self):
        """Positive: investigate_user_access V2 hints reference role bindings."""
        response = self._call_tool("investigate_user_access", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        self.assertIn("list_user_bindings", tool_output["hints"])

    def test_investigate_user_access_v2_full_permission_format(self):
        """Positive: investigate_user_access V2 handles full permission format."""
        response = self._call_tool(
            "investigate_user_access",
            {
                "username": self.test_username,
                "application": "compliance",
                "expected_permission": "compliance:policies:read",
            },
        )

        tool_output = self._get_tool_output(response)
        self.assertTrue(tool_output["analysis"]["has_expected_permission"])


class MCPWriteGatingTests(MCPToolTestMixin, IdentityRequest):
    """Test MCP_WRITE_ENABLED flag gating for write tools."""

    def setUp(self):
        """Set up write gating tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)

    def tearDown(self):
        """Tear down write gating tests."""
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    def test_write_tools_annotated_when_disabled(self):
        """Write tools are listed with disabled annotation when MCP_WRITE_ENABLED=False."""
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 2, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)
        tools = {t["name"]: t["description"] for t in response.json()["result"]["tools"]}
        self.assertIn("create_group", tools)
        self.assertTrue(tools["create_group"].startswith("[DISABLED -- write mode off]"))
        self.assertIn("add_principals_to_group", tools)
        self.assertTrue(tools["add_principals_to_group"].startswith("[DISABLED -- write mode off]"))

    @override_settings(MCP_WRITE_ENABLED=True)
    def test_write_tools_visible_when_enabled(self):
        """Write tools are listed when MCP_WRITE_ENABLED=True."""
        tool_names = self._get_tool_names()
        self.assertIn("create_group", tool_names)
        self.assertIn("add_principals_to_group", tool_names)
        self.assertIn("create_cross_account_request", tool_names)

    def test_write_tool_call_rejected_when_disabled(self):
        """Calling a write tool returns error when MCP_WRITE_ENABLED=False."""
        response = self._call_tool("create_group", {"name": "Test Group"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertIn("write mode is disabled", data["error"]["message"].lower())

    @override_settings(MCP_WRITE_ENABLED=True)
    def test_write_tool_call_requires_auth(self):
        """Write tools require authentication even when write mode is enabled."""
        response = self._call_tool("create_group", {"name": "Test Group"}, use_auth=False)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_read_tools_still_visible_when_write_disabled(self):
        """Read-only tools remain visible when MCP_WRITE_ENABLED=False."""
        tool_names = self._get_tool_names()
        self.assertIn("list_principals", tool_names)
        self.assertIn("list_groups", tool_names)
        self.assertIn("hello", tool_names)

    @override_settings(MCP_WRITE_ENABLED=True)
    def test_v1_write_tools_hidden_when_v2_disabled(self):
        """V1 write tools are listed, but V2 write tools are hidden when V2 is disabled."""
        tool_names = self._get_tool_names()
        self.assertIn("create_group", tool_names)
        self.assertIn("add_roles_to_group", tool_names)
        self.assertNotIn("create_role", tool_names)
        self.assertNotIn("create_role_bindings", tool_names)
        self.assertNotIn("create_workspace", tool_names)

    def test_write_config_on_tools(self):
        """All write tools have write=True in their config."""
        write_tool_names = [
            "create_group",
            "add_principals_to_group",
            "add_roles_to_group",
            "create_role_v1",
            "create_role",
            "create_role_bindings",
            "create_workspace",
            "create_cross_account_request",
            "update_group",
            "update_role_v1",
            "patch_role_v1",
            "update_role",
            "update_role_binding",
            "update_workspace",
            "move_workspace",
            "update_cross_account_request",
            "patch_cross_account_request",
            "delete_group",
            "remove_principals_from_group",
            "remove_roles_from_group",
            "delete_role_v1",
            "bulk_delete_roles",
            "delete_workspace",
        ]
        for name in write_tool_names:
            config = _TOOL_CONFIG.get(name)
            self.assertIsNotNone(config, f"Tool '{name}' not registered")
            self.assertTrue(config.write, f"Tool '{name}' should have write=True")
            self.assertTrue(config.requires_auth, f"Tool '{name}' should have requires_auth=True")


@override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False)
class MCPCreateGroupTests(MCPToolTestMixin, IdentityRequest):
    """Test create_group write tool."""

    def setUp(self):
        """Set up create group tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)

    def tearDown(self):
        """Tear down create group tests."""
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    def test_create_group_success(self):
        """Create a group successfully."""
        response = self._call_tool("create_group", {"name": "New Test Group", "description": "A test group"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output["name"], "New Test Group")
        self.assertEqual(output["description"], "A test group")
        self.assertIn("uuid", output)

    def test_create_group_missing_name(self):
        """Creating a group without required 'name' returns a param error."""
        response = self._call_tool("create_group", {})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32602)

    def test_create_group_no_auth(self):
        """Creating a group without auth returns auth error."""
        response = self._call_tool("create_group", {"name": "Test"}, use_auth=False)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)


@override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False)
class MCPAddPrincipalsToGroupTests(MCPToolTestMixin, IdentityRequest):
    """Test add_principals_to_group write tool."""

    def setUp(self):
        """Set up add principals tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        V2TenantBootstrapService(NoopReplicator()).bootstrap_tenant(self.tenant)
        self.group = Group.objects.create(name="Test Group", tenant=self.tenant)

    def tearDown(self):
        """Tear down add principals tests."""
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_add_principals_by_uuid(self, mock_proxy, mock_replicator):
        """Add principals to a group by UUID."""
        mock_proxy.return_value = {
            "status_code": 200,
            "data": [
                {
                    "username": "newuser",
                    "email": "new@example.com",
                    "first_name": "New",
                    "last_name": "User",
                    "is_org_admin": False,
                    "user_id": "12345",
                }
            ],
        }
        response = self._call_tool(
            "add_principals_to_group",
            {"group_uuid": str(self.group.uuid), "principals": ["newuser"]},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"])

    def test_add_principals_by_name(self):
        """Resolving group by name works."""
        response = self._call_tool(
            "add_principals_to_group",
            {"group_name": "Test Group", "principals": ["newuser"]},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"])

    def test_add_principals_missing_group(self):
        """Adding to a non-existent group returns error."""
        response = self._call_tool(
            "add_principals_to_group",
            {"group_name": "Nonexistent", "principals": ["user1"]},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertIn("error", output)
        self.assertIn("not found", output["error"])

    def test_add_principals_no_group_specified(self):
        """Omitting both group_uuid and group_name returns error."""
        response = self._call_tool(
            "add_principals_to_group",
            {"principals": ["user1"]},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertIn("error", output)
        self.assertIn("required", output["error"].lower())


@override_settings(
    MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False, V2_APIS_ENABLED=True, ATOMIC_RETRY_DISABLED=True
)
class MCPWriteToolsV2Tests(MCPToolTestMixin, IdentityRequest):
    """Test V2 write tools (create_role, create_workspace).

    Uses ATOMIC_RETRY_DISABLED=True so pgtransaction.atomic(retry=...)
    falls back to plain transaction.atomic(), avoiding nesting issues
    with TestCase's wrapping transaction.
    """

    def setUp(self):
        """Set up V2 write tool tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        self.enterContext(
            patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
        )
        V2TenantBootstrapService(NoopReplicator()).bootstrap_tenant(self.tenant)
        TenantMapping.objects.update_or_create(tenant=self.tenant, defaults={"v2_write_activated_at": timezone.now()})
        Permission.objects.create(
            application="rbac",
            resource_type="group",
            verb="read",
            permission="rbac:group:read",
            tenant=self.tenant,
        )
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )

    def tearDown(self):
        """Tear down V2 write tool tests."""
        RoleBinding.objects.all().delete()
        Workspace.objects.filter(type=Workspace.Types.STANDARD).delete()
        RoleV2.objects.all().delete()
        Permission.objects.filter(permission="rbac:group:read").delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()
        TenantMapping.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    def test_v2_write_tools_visible_when_both_flags_enabled(self):
        """V2 write tools appear when both MCP_WRITE_ENABLED and V2_APIS_ENABLED are True."""
        tool_names = self._get_tool_names()
        self.assertIn("create_role", tool_names)
        self.assertIn("create_role_bindings", tool_names)
        self.assertIn("create_workspace", tool_names)

    @patch("management.permissions.role_v2_access.RoleV2KesselAccessPermission.has_permission")
    def test_create_role_v2_success(self, mock_perm):
        """Create a V2 role successfully."""
        mock_perm.return_value = True
        response = self._call_tool(
            "create_role",
            {
                "name": "Test V2 Role",
                "description": "A test role",
                "permissions": [
                    {"application": "rbac", "resource_type": "group", "operation": "read"},
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"], f"Tool returned error: {data['result']}")
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output.get("name"), "Test V2 Role", f"Got output: {output}")

    @patch("management.permissions.workspace_access.WorkspaceAccessPermission.has_permission")
    def test_create_workspace_success(self, mock_perm):
        """Create a workspace successfully."""
        mock_perm.return_value = True
        root_ws = Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).first()
        self.assertIsNotNone(root_ws, "Root workspace should exist after bootstrap")

        from management.workspace.view import WorkspaceViewSet

        original_create_atomic = WorkspaceViewSet._create_atomic.__wrapped__

        with patch.object(WorkspaceViewSet, "_create_atomic", original_create_atomic):
            response = self._call_tool(
                "create_workspace",
                {
                    "name": "Test Workspace",
                    "description": "A test workspace",
                    "parent_id": str(root_ws.id),
                },
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"], f"Tool returned error: {data['result']}")
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output.get("name"), "Test Workspace", f"Got output: {output}")

    @patch("management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission")
    @patch("management.permissions.role_binding_access.RoleBindingSystemUserAccessPermission.has_permission")
    def test_create_role_bindings_success(self, mock_sys_perm, mock_kessel_perm):
        """Create a role binding successfully."""
        mock_sys_perm.return_value = True
        mock_kessel_perm.return_value = True
        role = RoleV2.objects.create(name="Binding Test Role", tenant=self.tenant)
        root_ws = Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).first()
        response = self._call_tool(
            "create_role_bindings",
            {
                "bindings": [
                    {
                        "role": {"id": str(role.id)},
                        "resource": {"type": "workspace", "id": str(root_ws.id)},
                        "subject": {"type": "principal", "id": str(self.principal.uuid)},
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"], f"Tool returned error: {data['result']}")


@override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False)
class MCPWriteToolsV1Tests(MCPToolTestMixin, IdentityRequest):
    """Test V1-only write tools (add_roles_to_group, create_role_v1)."""

    def setUp(self):
        """Set up V1 write tool tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        V2TenantBootstrapService(NoopReplicator()).bootstrap_tenant(self.tenant)
        self.group = Group.objects.create(name="Test Group", tenant=self.tenant)
        self.permission = Permission.objects.create(
            application="cost-management",
            resource_type="cost_model",
            verb="read",
            permission="cost-management:cost_model:read",
            tenant=self.tenant,
        )

    def tearDown(self):
        """Tear down V1 write tool tests."""
        Role.objects.all().delete()
        Group.objects.all().delete()
        Permission.objects.filter(permission="cost-management:cost_model:read").delete()
        Principal.objects.all().delete()
        super().tearDown()

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
    def test_add_roles_to_group_success(self, mock_replicator):
        """Add roles to a group successfully."""
        role = Role.objects.create(name="Test Role", tenant=self.tenant, system=False)
        Access.objects.create(role=role, permission=self.permission, tenant=self.tenant)
        response = self._call_tool(
            "add_roles_to_group",
            {"group_name": "Test Group", "roles": [str(role.uuid)]},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"], f"Tool returned error: {data['result']}")

    def test_create_role_v1_success(self):
        """Create a V1 role successfully."""
        response = self._call_tool(
            "create_role_v1",
            {
                "name": "MCP Created Role",
                "access": [{"permission": "cost-management:cost_model:read", "resourceDefinitions": []}],
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"], f"Tool returned error: {data['result']}")
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output.get("name"), "MCP Created Role", f"Got output: {output}")


@override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False)
class MCPCrossAccountRequestTests(MCPToolTestMixin, IdentityRequest):
    """Test create_cross_account_request write tool."""

    def setUp(self):
        """Set up cross-account request tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        self.target_tenant = Tenant.objects.create(
            tenant_name="target_org",
            org_id="9999999",
        )
        self.role = Role.objects.create(name="CAR Test Role", system=True, tenant=self.tenant)

    def tearDown(self):
        """Tear down cross-account request tests."""
        CrossAccountRequest.objects.all().delete()
        Role.objects.filter(name="CAR Test Role").delete()
        Principal.objects.all().delete()
        self.target_tenant.delete()
        super().tearDown()

    def test_create_cross_account_request_success(self):
        """Create a cross-account request successfully."""
        response = self._call_tool(
            "create_cross_account_request",
            {
                "target_org": "9999999",
                "start_date": "2026-06-01",
                "end_date": "2026-06-30",
                "roles": [str(self.role.uuid)],
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")


@override_settings(BYPASS_BOP_VERIFICATION=True)
class MCPGuideUserAccessDelegationTests(MCPToolTestMixin, IdentityRequest):
    """Tests for the guide_user_access_delegation MCP tool."""

    def setUp(self):
        """Set up guide_user_access_delegation tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.test_username = self.user_data["username"]
        self.principal = Principal.objects.create(username=self.test_username, tenant=self.tenant)

        self.user_access_admin_role = Role.objects.create(
            name="User Access administrator",
            display_name="User Access administrator",
            description="Provides access to create and manage roles, groups, and principals in RBAC.",
            system=True,
            tenant=Tenant.objects.get(tenant_name="public"),
        )

        self.rbac_permission = Permission.objects.create(
            application="rbac",
            resource_type="*",
            verb="*",
            permission="rbac:*:*",
            tenant=Tenant.objects.get(tenant_name="public"),
        )
        self.rbac_access = Access.objects.create(
            permission=self.rbac_permission,
            role=self.user_access_admin_role,
            tenant=Tenant.objects.get(tenant_name="public"),
        )

        self.access_governance_group = Group.objects.create(
            name="Access Governance",
            description="Team managing user access",
            tenant=self.tenant,
        )
        self.access_policy = Policy.objects.create(
            name="access_governance_policy",
            group=self.access_governance_group,
            tenant=self.tenant,
        )
        self.access_policy.roles.add(self.user_access_admin_role)

        self.admin_user1 = Principal.objects.create(username="admin_user1", tenant=self.tenant)
        self.admin_user2 = Principal.objects.create(username="admin_user2", tenant=self.tenant)
        self.access_governance_group.principals.add(self.admin_user1)
        self.access_governance_group.principals.add(self.admin_user2)

    def tearDown(self):
        """Tear down guide_user_access_delegation tests."""
        Policy.objects.all().delete()
        Access.objects.all().delete()
        Role.objects.all().delete()
        Permission.objects.all().delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    def test_guide_user_access_delegation_success(self):
        """Positive: guide_user_access_delegation returns factual data."""
        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        tool_output = self._get_tool_output(response)

        # Verify simplified structure
        self.assertIn("org_version", tool_output)
        self.assertIn("user_info", tool_output)
        self.assertIn("role_info", tool_output)
        self.assertIn("user_already_has_role", tool_output)
        self.assertIn("existing_assignments", tool_output)

    def test_guide_user_access_delegation_finds_role(self):
        """Positive: guide_user_access_delegation finds the User Access administrator role."""
        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        # Verify role info (simplified - only uuid and name)
        self.assertIsNotNone(tool_output["role_info"])
        self.assertEqual(tool_output["role_info"]["name"], "User Access administrator")
        self.assertEqual(tool_output["role_info"]["uuid"], str(self.user_access_admin_role.uuid))

    def test_guide_user_access_delegation_finds_groups_with_role(self):
        """Positive: guide_user_access_delegation finds groups that have the role."""
        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        # Verify existing_assignments contains the group
        self.assertEqual(len(tool_output["existing_assignments"]), 1)
        assignment = tool_output["existing_assignments"][0]
        self.assertEqual(assignment["type"], "group")
        self.assertEqual(assignment["name"], "Access Governance")

    def test_guide_user_access_delegation_user_not_org_admin(self):
        """Positive: guide_user_access_delegation shows user is not org admin."""
        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        # Verify user_info shows is_org_admin status
        self.assertIn("is_org_admin", tool_output["user_info"])

    def test_guide_user_access_delegation_without_auth_returns_error(self):
        """Permission: guide_user_access_delegation without auth returns auth error."""
        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username}, use_auth=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    @patch("management.mcp_views._list_principals_by_name")
    @patch("management.mcp_views.list_principals")
    def test_guide_user_access_delegation_nonexistent_user(self, mock_list_principals, mock_name_search):
        """Edge case: guide_user_access_delegation handles non-existent user gracefully."""
        mock_list_principals.return_value = '{"data": []}'
        mock_name_search.return_value = '{"data": []}'

        response = self._call_tool("guide_user_access_delegation", {"username": "nonexistent_user"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        tool_output = self._get_tool_output(response)

        self.assertIsNotNone(tool_output["role_info"])
        self.assertFalse(tool_output["user_already_has_role"])
        self.assertIn("error", tool_output["user_info"])

    def test_guide_user_access_delegation_no_groups_with_role(self):
        """Edge case: handles when no groups have the role assigned."""
        # Remove the role from the group
        self.access_policy.roles.remove(self.user_access_admin_role)

        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        # Should have empty existing_assignments
        self.assertEqual(len(tool_output["existing_assignments"]), 0)

    def test_guide_user_access_delegation_tool_in_tools_list(self):
        """Positive: guide_user_access_delegation appears in tools/list."""
        tool_names = self._get_tool_names()
        self.assertIn("guide_user_access_delegation", tool_names)

    def test_guide_user_access_delegation_user_already_has_role(self):
        """Positive: detects when user already has the role via group membership."""
        # Add the test user to the group that has the role
        self.access_governance_group.principals.add(self.principal)

        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        # Should detect that user already has the role
        self.assertTrue(tool_output["user_already_has_role"])

    def test_guide_user_access_delegation_user_does_not_have_role(self):
        """Positive: correctly identifies when user does not have the role."""
        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        # User should not have the role (not in the group)
        self.assertFalse(tool_output["user_already_has_role"])

    def test_guide_user_access_delegation_role_not_found(self):
        """Edge case: handles when User Access administrator role doesn't exist."""
        # Delete the role
        self.rbac_access.delete()
        self.user_access_admin_role.delete()

        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        # Should indicate role not found
        self.assertIn("error", tool_output["role_info"])
        self.assertIn("not found", tool_output["role_info"]["error"].lower())

    def test_guide_user_access_delegation_multiple_groups_with_role(self):
        """Positive: lists multiple groups when several have the role."""
        # Create another group with the same role
        second_group = Group.objects.create(
            name="Security Team",
            description="Security administrators",
            tenant=self.tenant,
        )
        second_policy = Policy.objects.create(
            name="security_team_policy",
            group=second_group,
            tenant=self.tenant,
        )
        second_policy.roles.add(self.user_access_admin_role)

        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        # Should list both groups in existing_assignments
        self.assertEqual(len(tool_output["existing_assignments"]), 2)
        group_names = [a["name"] for a in tool_output["existing_assignments"]]
        self.assertIn("Access Governance", group_names)
        self.assertIn("Security Team", group_names)

    @patch("management.mcp_views._list_principals_by_name")
    @patch("management.mcp_views.list_principals")
    def test_guide_user_access_delegation_fuzzy_single_match(self, mock_list_principals, mock_name_search):
        """Positive: fuzzy fallback resolves display name to single user."""
        mock_list_principals.return_value = '{"data": []}'
        mock_name_search.return_value = json.dumps(
            {
                "data": [
                    {
                        "username": self.test_username,
                        "first_name": "Joe",
                        "last_name": "Doe",
                        "is_org_admin": False,
                        "is_active": True,
                    }
                ]
            }
        )

        response = self._call_tool("guide_user_access_delegation", {"username": "Joe"})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["user_info"]["username"], self.test_username)
        self.assertIn("resolved_from", tool_output["user_info"])

    @patch("management.mcp_views._list_principals_by_name")
    @patch("management.mcp_views.list_principals")
    def test_guide_user_access_delegation_fuzzy_multiple_matches(self, mock_list_principals, mock_name_search):
        """Edge case: fuzzy fallback returns candidates when multiple users match."""
        mock_list_principals.return_value = '{"data": []}'
        mock_name_search.return_value = json.dumps(
            {
                "data": [
                    {"username": "joedoe1", "first_name": "Joe", "last_name": "Doe"},
                    {"username": "joedoe2", "first_name": "Joe", "last_name": "Smith"},
                ]
            }
        )

        response = self._call_tool("guide_user_access_delegation", {"username": "Joe"})

        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output["user_info"])
        self.assertIn("candidates", tool_output["user_info"])
        self.assertEqual(len(tool_output["user_info"]["candidates"]), 2)
        self.assertIn("hint", tool_output["user_info"])

    @patch("management.mcp_views._list_principals_by_name")
    @patch("management.mcp_views.list_principals")
    def test_guide_user_access_delegation_fuzzy_no_match(self, mock_list_principals, mock_name_search):
        """Negative: fuzzy fallback returns error when no users match."""
        mock_list_principals.return_value = '{"data": []}'
        mock_name_search.return_value = '{"data": []}'

        response = self._call_tool("guide_user_access_delegation", {"username": "Nonexistent"})

        tool_output = self._get_tool_output(response)
        self.assertIn("error", tool_output["user_info"])
        self.assertNotIn("candidates", tool_output["user_info"])

    def test_guide_user_access_delegation_v1_org_version(self):
        """Positive: V1 organization shows org_version=v1."""
        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["org_version"], "v1")


@override_settings(BYPASS_BOP_VERIFICATION=True, V2_APIS_ENABLED=True)
class MCPGuideUserAccessDelegationV2Tests(MCPToolTestMixin, IdentityRequest):
    """Tests for guide_user_access_delegation on V2 organizations."""

    def setUp(self):
        """Set up V2 guide_user_access_delegation tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.test_username = self.user_data["username"]
        self.principal = Principal.objects.create(username=self.test_username, tenant=self.tenant)
        self.enterContext(
            patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
        )
        V2TenantBootstrapService(NoopReplicator()).bootstrap_tenant(self.tenant)
        TenantMapping.objects.update_or_create(tenant=self.tenant, defaults={"v2_write_activated_at": timezone.now()})
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )

        self.public_tenant = Tenant.objects.get(tenant_name="public")
        self.user_access_admin_role = Role.objects.create(
            name="User Access administrator",
            display_name="User Access administrator",
            description="Provides access to create and manage roles, groups, and principals in RBAC.",
            system=True,
            tenant=self.public_tenant,
        )

        self.rbac_permission = Permission.objects.create(
            application="rbac",
            resource_type="*",
            verb="*",
            permission="rbac:*:*",
            tenant=self.public_tenant,
        )
        self.rbac_access = Access.objects.create(
            permission=self.rbac_permission,
            role=self.user_access_admin_role,
            tenant=self.public_tenant,
        )

        self.user_access_admin_role_v2 = RoleV2.objects.create(
            name="User Access administrator",
            tenant=self.tenant,
        )

        self.binding = RoleBinding.objects.create(
            tenant=self.tenant,
            role=self.user_access_admin_role_v2,
            resource_type="workspace",
            resource_id="root-workspace-id",
        )

        self.admin_principal = Principal.objects.create(username="admin_via_binding", tenant=self.tenant)
        RoleBindingPrincipal.objects.create(binding=self.binding, principal=self.admin_principal, source="direct")

        self.admin_group = Group.objects.create(name="User Access Admins Group", tenant=self.tenant)

    def tearDown(self):
        """Tear down V2 guide_user_access_delegation tests."""
        RoleBindingPrincipal.objects.all().delete()
        RoleBindingGroup.objects.all().delete()
        RoleBinding.objects.all().delete()
        RoleV2.objects.all().delete()
        Policy.objects.all().delete()
        Access.objects.all().delete()
        Role.objects.all().delete()
        Permission.objects.all().delete()
        TenantMapping.objects.all().delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    def test_guide_user_access_delegation_v2_returns_org_version(self):
        """Positive: V2 organization shows org_version=v2."""
        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)
        self.assertEqual(tool_output["org_version"], "v2")

    def test_guide_user_access_delegation_v2_finds_role_bindings(self):
        """Positive: V2 org finds role bindings with the role."""
        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        self.assertEqual(len(tool_output["existing_assignments"]), 1)
        assignment = tool_output["existing_assignments"][0]
        self.assertEqual(assignment["type"], "role_binding")
        self.assertEqual(assignment["principals"], 1)

    def test_guide_user_access_delegation_v2_user_has_role_direct(self):
        """Positive: V2 detects user has role via direct binding."""
        RoleBindingPrincipal.objects.create(binding=self.binding, principal=self.principal, source="direct")

        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        self.assertTrue(tool_output["user_already_has_role"])

    def test_guide_user_access_delegation_v2_user_has_role_via_group(self):
        """Positive: V2 detects user has role via group membership."""
        self.admin_group.principals.add(self.principal)
        RoleBindingGroup.objects.create(binding=self.binding, group=self.admin_group)

        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        self.assertTrue(tool_output["user_already_has_role"])

    def test_guide_user_access_delegation_v2_user_does_not_have_role(self):
        """Positive: V2 correctly identifies user does not have the role."""
        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        self.assertFalse(tool_output["user_already_has_role"])

    def test_guide_user_access_delegation_v2_no_bindings(self):
        """Edge case: V2 org with no role bindings for the role."""
        RoleBindingPrincipal.objects.all().delete()
        self.binding.delete()

        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        self.assertEqual(len(tool_output["existing_assignments"]), 0)

    def test_guide_user_access_delegation_v2_multiple_bindings(self):
        """Positive: V2 lists multiple role bindings when several exist."""
        second_binding = RoleBinding.objects.create(
            tenant=self.tenant,
            role=self.user_access_admin_role_v2,
            resource_type="workspace",
            resource_id="child-workspace-id",
        )
        RoleBindingPrincipal.objects.create(binding=second_binding, principal=self.admin_principal, source="direct")

        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        self.assertEqual(len(tool_output["existing_assignments"]), 2)

    def test_guide_user_access_delegation_v2_shows_binding_counts(self):
        """Positive: V2 role binding info includes principal and group counts."""
        RoleBindingGroup.objects.create(binding=self.binding, group=self.admin_group)

        response = self._call_tool("guide_user_access_delegation", {"username": self.test_username})

        tool_output = self._get_tool_output(response)

        assignment = tool_output["existing_assignments"][0]
        self.assertEqual(assignment["principals"], 1)
        self.assertEqual(assignment["groups"], 1)


# --- UPDATE tool tests ---


@override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False)
class MCPUpdateGroupTests(MCPToolTestMixin, IdentityRequest):
    """Test update_group write tool."""

    def setUp(self):
        """Set up update group tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        V2TenantBootstrapService(NoopReplicator()).bootstrap_tenant(self.tenant)
        self.group = Group.objects.create(name="Test Group", description="Original desc", tenant=self.tenant)

    def tearDown(self):
        """Tear down update group tests."""
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
    def test_update_group_success(self, mock_replicator):
        """Update a group successfully."""
        response = self._call_tool(
            "update_group",
            {"group_uuid": str(self.group.uuid), "name": "Updated Group", "description": "New desc"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output["name"], "Updated Group")

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
    def test_update_group_by_name(self, mock_replicator):
        """Update a group resolved by name."""
        response = self._call_tool(
            "update_group",
            {"group_name": "Test Group", "name": "Renamed Group"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"])

    def test_update_group_no_auth(self):
        """Updating a group without auth returns auth error."""
        response = self._call_tool("update_group", {"name": "Test"}, use_auth=False)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_update_group_not_found(self):
        """Updating a non-existent group returns error."""
        response = self._call_tool(
            "update_group",
            {"group_name": "Nonexistent", "name": "New Name"},
        )

        self.assertEqual(response.status_code, 200)
        output = self._get_tool_output(response)
        self.assertIn("error", output)


@override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False)
class MCPUpdateRoleV1Tests(MCPToolTestMixin, IdentityRequest):
    """Test update_role_v1 and patch_role_v1 write tools."""

    def setUp(self):
        """Set up V1 role update tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        V2TenantBootstrapService(NoopReplicator()).bootstrap_tenant(self.tenant)
        self.permission = Permission.objects.create(
            application="cost-management",
            resource_type="cost_model",
            verb="read",
            permission="cost-management:cost_model:read",
            tenant=self.tenant,
        )
        self.role = Role.objects.create(name="Test Role", tenant=self.tenant, system=False)
        Access.objects.create(role=self.role, permission=self.permission, tenant=self.tenant)

    def tearDown(self):
        """Tear down V1 role update tests."""
        Role.objects.all().delete()
        Permission.objects.filter(permission="cost-management:cost_model:read").delete()
        Principal.objects.all().delete()
        super().tearDown()

    def test_update_role_v1_success(self):
        """Update a V1 role successfully."""
        response = self._call_tool(
            "update_role_v1",
            {
                "role_uuid": str(self.role.uuid),
                "name": "Updated Role",
                "access": [{"permission": "cost-management:cost_model:read", "resourceDefinitions": []}],
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"], f"Tool returned error: {data['result']}")
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output.get("name"), "Updated Role")

    def test_update_role_v1_no_auth(self):
        """Updating a role without auth returns auth error."""
        response = self._call_tool(
            "update_role_v1",
            {"role_uuid": str(self.role.uuid), "name": "X", "access": []},
            use_auth=False,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_patch_role_v1_success(self):
        """Partially update a V1 role successfully."""
        response = self._call_tool(
            "patch_role_v1",
            {"role_uuid": str(self.role.uuid), "display_name": "New Display Name"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"], f"Tool returned error: {data['result']}")

    def test_patch_role_v1_no_fields(self):
        """Patching a role without any fields returns error."""
        response = self._call_tool(
            "patch_role_v1",
            {"role_uuid": str(self.role.uuid)},
        )

        self.assertEqual(response.status_code, 200)
        output = self._get_tool_output(response)
        self.assertIn("error", output)
        self.assertIn("required", output["error"].lower())


@override_settings(
    MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False, V2_APIS_ENABLED=True, ATOMIC_RETRY_DISABLED=True
)
class MCPUpdateToolsV2Tests(MCPToolTestMixin, IdentityRequest):
    """Test V2 update tools (update_role, update_role_binding, update_workspace, move_workspace)."""

    def setUp(self):
        """Set up V2 update tool tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )
        V2TenantBootstrapService(NoopReplicator()).bootstrap_tenant(self.tenant)
        TenantMapping.objects.update_or_create(tenant=self.tenant, defaults={"v2_write_activated_at": timezone.now()})
        self.permission_obj = Permission.objects.create(
            application="rbac",
            resource_type="group",
            verb="read",
            permission="rbac:group:read",
            tenant=self.tenant,
        )

    def tearDown(self):
        """Tear down V2 update tool tests."""
        RoleBinding.objects.all().delete()
        for ws in Workspace.objects.filter(type=Workspace.Types.STANDARD).order_by("-parent_id"):
            ws.delete()
        RoleV2.objects.all().delete()
        Permission.objects.filter(permission="rbac:group:read").delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()
        TenantMapping.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    @patch("management.permissions.role_v2_access.RoleV2KesselAccessPermission.has_permission")
    def test_update_role_v2_success(self, mock_perm):
        """Update a V2 role successfully."""
        mock_perm.return_value = True
        role = RoleV2.objects.create(name="Original Role", tenant=self.tenant)

        response = self._call_tool(
            "update_role",
            {
                "role_uuid": str(role.uuid),
                "name": "Updated V2 Role",
                "permissions": [
                    {"application": "rbac", "resource_type": "group", "operation": "read"},
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"], f"Tool returned error: {data['result']}")
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output.get("name"), "Updated V2 Role")

    def test_update_role_v2_no_auth(self):
        """Updating a V2 role without auth returns auth error."""
        response = self._call_tool(
            "update_role",
            {"role_uuid": "fake", "name": "X", "permissions": []},
            use_auth=False,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    @patch("management.permissions.workspace_access.WorkspaceAccessPermission.has_permission")
    def test_update_workspace_success(self, mock_perm):
        """Update a workspace successfully."""
        mock_perm.return_value = True
        root_ws = Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).first()
        ws = Workspace.objects.create(
            name="Test WS", tenant=self.tenant, type=Workspace.Types.STANDARD, parent=root_ws
        )

        response = self._call_tool(
            "update_workspace",
            {
                "workspace_id": str(ws.id),
                "name": "Updated WS",
                "description": "Updated",
                "parent_id": str(root_ws.id),
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"], f"Tool returned error: {data['result']}")
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output.get("name"), "Updated WS")

    @patch("management.permissions.workspace_access.WorkspaceAccessPermission.has_permission")
    def test_move_workspace_success(self, mock_perm):
        """Move a workspace to a new parent."""
        mock_perm.return_value = True
        root_ws = Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).first()
        parent1 = Workspace.objects.create(
            name="Parent 1", tenant=self.tenant, type=Workspace.Types.STANDARD, parent=root_ws
        )
        child = Workspace.objects.create(
            name="Child WS", tenant=self.tenant, type=Workspace.Types.STANDARD, parent=parent1
        )
        parent2 = Workspace.objects.create(
            name="Parent 2", tenant=self.tenant, type=Workspace.Types.STANDARD, parent=root_ws
        )

        from management.workspace.view import WorkspaceViewSet

        original_move = WorkspaceViewSet._move_atomic.__wrapped__
        with patch.object(WorkspaceViewSet, "_move_atomic", original_move):
            response = self._call_tool(
                "move_workspace",
                {"workspace_id": str(child.id), "parent_id": str(parent2.id)},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"], f"Tool returned error: {data['result']}")

    @patch("management.permissions.role_binding_access.RoleBindingKesselAccessPermission.has_permission")
    @patch("management.permissions.role_binding_access.RoleBindingSystemUserAccessPermission.has_permission")
    def test_update_role_binding_success(self, mock_sys_perm, mock_kessel_perm):
        """Update role bindings for a subject."""
        mock_sys_perm.return_value = True
        mock_kessel_perm.return_value = True
        role = RoleV2.objects.create(name="Binding Role", tenant=self.tenant)
        root_ws = Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).first()

        response = self._call_tool(
            "update_role_binding",
            {
                "resource_id": str(root_ws.id),
                "resource_type": "workspace",
                "subject_id": str(self.principal.uuid),
                "subject_type": "principal",
                "roles": [{"id": str(role.uuid)}],
            },
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"], f"Tool returned error: {data['result']}")


@override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False)
class MCPUpdateCrossAccountTests(MCPToolTestMixin, IdentityRequest):
    """Test update_cross_account_request and patch_cross_account_request."""

    def setUp(self):
        """Set up cross-account update tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)

    def tearDown(self):
        """Tear down cross-account update tests."""
        Principal.objects.all().delete()
        super().tearDown()

    def test_patch_cross_account_request_no_auth(self):
        """Patching without auth returns auth error."""
        response = self._call_tool(
            "patch_cross_account_request",
            {"request_id": "00000000-0000-0000-0000-000000000000", "status": "cancelled"},
            use_auth=False,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_update_cross_account_request_no_auth(self):
        """Updating without auth returns auth error."""
        response = self._call_tool(
            "update_cross_account_request",
            {
                "request_id": "00000000-0000-0000-0000-000000000000",
                "target_org": "12345",
                "start_date": "06/01/2026",
                "end_date": "06/30/2026",
                "roles": ["Vulnerability administrator"],
            },
            use_auth=False,
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_patch_cross_account_request_registered(self):
        """patch_cross_account_request is registered as a write tool."""
        config = _TOOL_CONFIG.get("patch_cross_account_request")
        self.assertIsNotNone(config)
        self.assertTrue(config.write)
        self.assertTrue(config.requires_auth)

    def test_update_cross_account_request_registered(self):
        """update_cross_account_request is registered as a write tool."""
        config = _TOOL_CONFIG.get("update_cross_account_request")
        self.assertIsNotNone(config)
        self.assertTrue(config.write)
        self.assertTrue(config.requires_auth)

    def test_patch_cross_account_request_rejects_invalid_status(self):
        """patch_cross_account_request rejects invalid status values."""
        response = self._call_tool(
            "patch_cross_account_request",
            {"request_id": "00000000-0000-0000-0000-000000000000", "status": "foo"},
        )

        self.assertEqual(response.status_code, 200)
        output = self._get_tool_output(response)
        self.assertIn("error", output)
        self.assertIn("foo", output["error"])
        self.assertIn("approved", output["error"])


# --- DELETE tool tests ---


@override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False)
class MCPDeleteGroupTests(MCPToolTestMixin, IdentityRequest):
    """Test delete_group and remove_principals_from_group write tools."""

    def setUp(self):
        """Set up delete group tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        V2TenantBootstrapService(NoopReplicator()).bootstrap_tenant(self.tenant)
        self.group = Group.objects.create(name="Delete Me", tenant=self.tenant)

    def tearDown(self):
        """Tear down delete group tests."""
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
    def test_delete_group_success(self, mock_replicator):
        """Delete a group successfully."""
        response = self._call_tool(
            "delete_group",
            {"group_uuid": str(self.group.uuid)},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output["status"], "no_content")

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
    def test_delete_group_by_name(self, mock_replicator):
        """Delete a group resolved by name."""
        response = self._call_tool(
            "delete_group",
            {"group_name": "Delete Me"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"])

    def test_delete_group_no_auth(self):
        """Deleting without auth returns auth error."""
        response = self._call_tool("delete_group", {"group_name": "Delete Me"}, use_auth=False)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    def test_delete_group_not_found(self):
        """Deleting a non-existent group returns error."""
        response = self._call_tool(
            "delete_group",
            {"group_name": "Nonexistent"},
        )

        self.assertEqual(response.status_code, 200)
        output = self._get_tool_output(response)
        self.assertIn("error", output)

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals")
    def test_remove_principals_from_group_success(self, mock_proxy, mock_replicator):
        """Remove principals from a group."""
        mock_proxy.return_value = {
            "status_code": 200,
            "data": [{"username": "testuser", "user_id": "12345", "is_org_admin": False}],
        }
        self.group.principals.add(self.principal)

        response = self._call_tool(
            "remove_principals_from_group",
            {"group_uuid": str(self.group.uuid), "usernames": "test_user"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output["status"], "no_content")

    def test_remove_principals_missing_params(self):
        """Removing without usernames or service_accounts returns error."""
        response = self._call_tool(
            "remove_principals_from_group",
            {"group_uuid": str(self.group.uuid)},
        )

        self.assertEqual(response.status_code, 200)
        output = self._get_tool_output(response)
        self.assertIn("error", output)
        self.assertIn("required", output["error"].lower())


@override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False)
class MCPDeleteRoleV1Tests(MCPToolTestMixin, IdentityRequest):
    """Test remove_roles_from_group and delete_role_v1 write tools."""

    def setUp(self):
        """Set up V1 role delete tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        V2TenantBootstrapService(NoopReplicator()).bootstrap_tenant(self.tenant)
        self.group = Group.objects.create(name="Test Group", tenant=self.tenant)
        self.permission = Permission.objects.create(
            application="cost-management",
            resource_type="cost_model",
            verb="read",
            permission="cost-management:cost_model:read",
            tenant=self.tenant,
        )
        self.role = Role.objects.create(name="Delete Me Role", tenant=self.tenant, system=False)
        Access.objects.create(role=self.role, permission=self.permission, tenant=self.tenant)

    def tearDown(self):
        """Tear down V1 role delete tests."""
        Role.objects.all().delete()
        Group.objects.all().delete()
        Permission.objects.filter(permission="cost-management:cost_model:read").delete()
        Principal.objects.all().delete()
        super().tearDown()

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
    def test_remove_roles_from_group_success(self, mock_replicator):
        """Remove roles from a group successfully."""
        policy = Policy.objects.create(name="test-policy", group=self.group, tenant=self.tenant)
        policy.roles.add(self.role)

        response = self._call_tool(
            "remove_roles_from_group",
            {"group_name": "Test Group", "roles": str(self.role.uuid)},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output["status"], "no_content")

    @patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
    def test_delete_role_v1_success(self, mock_replicator):
        """Delete a V1 role successfully."""
        response = self._call_tool(
            "delete_role_v1",
            {"role_uuid": str(self.role.uuid)},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output["status"], "no_content")

    def test_delete_role_v1_no_auth(self):
        """Deleting a role without auth returns auth error."""
        response = self._call_tool("delete_role_v1", {"role_uuid": str(self.role.uuid)}, use_auth=False)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)


@override_settings(
    MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False, V2_APIS_ENABLED=True, ATOMIC_RETRY_DISABLED=True
)
class MCPDeleteToolsV2Tests(MCPToolTestMixin, IdentityRequest):
    """Test V2 delete tools (bulk_delete_roles, delete_workspace)."""

    def setUp(self):
        """Set up V2 delete tool tests."""
        reload(urls)
        clear_url_caches()
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)
        self.enterContext(
            patch("management.relation_replicator.outbox_replicator.OutboxReplicator._save_replication_event")
        )
        V2TenantBootstrapService(NoopReplicator()).bootstrap_tenant(self.tenant)
        TenantMapping.objects.update_or_create(tenant=self.tenant, defaults={"v2_write_activated_at": timezone.now()})
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.get_kessel_principal_id",
                return_value="localhost/test-user-id",
            )
        )
        self.enterContext(
            patch(
                "management.permissions.role_v2_access.WorkspaceInventoryAccessChecker.check_resource_access",
                return_value=True,
            )
        )

    def tearDown(self):
        """Tear down V2 delete tool tests."""
        RoleBinding.objects.all().delete()
        for ws in Workspace.objects.filter(type=Workspace.Types.STANDARD).order_by("-parent_id"):
            ws.delete()
        RoleV2.objects.all().delete()
        Group.objects.all().delete()
        Principal.objects.all().delete()
        TenantMapping.objects.filter(tenant=self.tenant).delete()
        super().tearDown()

    @patch("management.permissions.role_v2_access.RoleV2KesselAccessPermission.has_permission")
    def test_bulk_delete_roles_success(self, mock_perm):
        """Bulk-delete V2 roles successfully."""
        mock_perm.return_value = True
        role1 = RoleV2.objects.create(name="Delete Role 1", tenant=self.tenant)
        role2 = RoleV2.objects.create(name="Delete Role 2", tenant=self.tenant)

        response = self._call_tool(
            "bulk_delete_roles",
            {"ids": [str(role1.uuid), str(role2.uuid)]},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"], f"Tool returned error: {data['result']}")
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output["status"], "no_content")

    def test_bulk_delete_roles_no_auth(self):
        """Bulk-deleting roles without auth returns auth error."""
        response = self._call_tool("bulk_delete_roles", {"ids": ["fake"]}, use_auth=False)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)

    @patch("management.permissions.workspace_access.WorkspaceAccessPermission.has_permission")
    def test_delete_workspace_success(self, mock_perm):
        """Delete a workspace successfully."""
        mock_perm.return_value = True
        root_ws = Workspace.objects.filter(tenant=self.tenant, type=Workspace.Types.ROOT).first()
        ws = Workspace.objects.create(
            name="Delete WS", tenant=self.tenant, type=Workspace.Types.STANDARD, parent=root_ws
        )

        response = self._call_tool(
            "delete_workspace",
            {"workspace_uuid": str(ws.id)},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data, f"Expected result but got: {data}")
        self.assertFalse(data["result"]["isError"], f"Tool returned error: {data['result']}")
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output["status"], "no_content")

    def test_delete_workspace_no_auth(self):
        """Deleting a workspace without auth returns auth error."""
        response = self._call_tool("delete_workspace", {"workspace_uuid": "fake"}, use_auth=False)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)


class MCPHealthCheckTests(MCPToolTestMixin, IdentityRequest):
    """Test the health_check MCP tool."""

    def setUp(self):
        """Set up health check tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()

    @patch("management.mcp_views._check_redis")
    def test_health_check_all_healthy(self, _mock_redis):
        """health_check returns status ok when all checks pass."""
        response = self._call_tool("health_check")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        output = self._get_tool_output(response)
        self.assertEqual(output["status"], "ok")
        self.assertEqual(output["checks"]["tools"], "ok")
        self.assertEqual(output["checks"]["database"], "ok")
        self.assertEqual(output["checks"]["redis"], "ok")
        self.assertNotIn("details", output)

    @patch("management.mcp_views._check_redis")
    def test_health_check_works_without_auth(self, _mock_redis):
        """health_check does not require authentication."""
        response = self._call_tool("health_check", use_auth=False)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data)
        self.assertFalse(data["result"]["isError"])
        output = self._get_tool_output(response)
        self.assertIn("status", output)
        self.assertIn("checks", output)

    @patch("management.mcp_views._check_redis")
    @patch("management.mcp_views._check_database", side_effect=Exception("connection refused"))
    def test_health_check_database_failure(self, _mock_db, _mock_redis):
        """health_check returns degraded when database is unreachable."""
        response = self._call_tool("health_check")

        self.assertEqual(response.status_code, 200)
        output = self._get_tool_output(response)
        self.assertEqual(output["status"], "degraded")
        self.assertEqual(output["checks"]["database"], "error")
        self.assertEqual(output["details"]["database"], "unavailable")
        # Other checks should still report independently
        self.assertEqual(output["checks"]["tools"], "ok")

    @patch("management.mcp_views._check_redis", side_effect=Exception("redis down"))
    def test_health_check_redis_failure(self, _mock_redis):
        """health_check returns degraded when Redis is unreachable."""
        response = self._call_tool("health_check")

        self.assertEqual(response.status_code, 200)
        output = self._get_tool_output(response)
        self.assertEqual(output["status"], "degraded")
        self.assertEqual(output["checks"]["redis"], "error")
        self.assertEqual(output["details"]["redis"], "unavailable")
        self.assertEqual(output["checks"]["tools"], "ok")
        self.assertEqual(output["checks"]["database"], "ok")

    @patch("management.mcp_views._check_redis")
    @patch("management.mcp_views._get_tools", side_effect=RuntimeError("event loop error"))
    def test_health_check_tool_registry_failure(self, _mock_tools, _mock_redis):
        """health_check returns degraded when tool registry cannot be resolved."""
        response = self._call_tool("health_check")

        self.assertEqual(response.status_code, 200)
        output = self._get_tool_output(response)
        self.assertEqual(output["status"], "degraded")
        self.assertEqual(output["checks"]["tools"], "error")
        self.assertEqual(output["details"]["tools"], "unavailable")

    @patch("management.mcp_views._get_tools", side_effect=RuntimeError("tools broken"))
    @patch("management.mcp_views._check_redis", side_effect=Exception("redis down"))
    @patch("management.mcp_views._check_database", side_effect=Exception("db down"))
    def test_health_check_all_degraded(self, _mock_db, _mock_redis, _mock_tools):
        """health_check returns degraded with all checks failing."""
        response = self._call_tool("health_check")

        self.assertEqual(response.status_code, 200)
        output = self._get_tool_output(response)
        self.assertEqual(output["status"], "degraded")
        self.assertEqual(output["checks"]["tools"], "error")
        self.assertEqual(output["checks"]["database"], "error")
        self.assertEqual(output["checks"]["redis"], "error")
        self.assertEqual(len(output["details"]), 3)
        for val in output["details"].values():
            self.assertEqual(val, "unavailable")


@override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False)
class MCPAuditLogSourceTests(MCPToolTestMixin, IdentityRequest):
    """Test that MCP write tools tag audit log entries with ai_assistant source."""

    def setUp(self):
        """Set up audit log source tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)

    def test_mcp_create_group_sets_audit_source(self):
        """MCP create_group tags audit log with ai_assistant source."""
        response = self._call_tool("create_group", {"name": "MCP Group", "description": "via MCP"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["result"]["isError"])

        audit_entry = AuditLog.objects.filter(
            tenant=self.tenant, resource_type=AuditLog.GROUP, action=AuditLog.CREATE
        ).first()
        self.assertIsNotNone(audit_entry)
        self.assertEqual(audit_entry.source, AuditLog.SOURCE_AI_ASSISTANT)

    def test_direct_api_write_has_no_source(self):
        """Direct API group creation does not set audit source."""
        AuditLog.objects.create(
            principal_username="api_user",
            resource_type=AuditLog.GROUP,
            action=AuditLog.CREATE,
            description="Created group: Direct Group",
            tenant=self.tenant,
        )

        audit_entry = AuditLog.objects.filter(tenant=self.tenant, principal_username="api_user").first()
        self.assertIsNotNone(audit_entry)
        self.assertIsNone(audit_entry.source)

    def test_list_audit_logs_includes_source_field(self):
        """list_audit_logs output includes source field."""
        AuditLog.objects.create(
            principal_username="mcp_user",
            resource_type=AuditLog.GROUP,
            action=AuditLog.CREATE,
            description="Created group: MCP Group",
            source=AuditLog.SOURCE_AI_ASSISTANT,
            tenant=self.tenant,
        )
        AuditLog.objects.create(
            principal_username="api_user",
            resource_type=AuditLog.GROUP,
            action=AuditLog.CREATE,
            description="Created group: API Group",
            tenant=self.tenant,
        )

        response = self._call_tool("list_audit_logs", {"limit": 10})

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        entries = tool_output["data"]
        self.assertEqual(len(entries), 2)

        mcp_entry = next(e for e in entries if e["principal_username"] == "mcp_user")
        api_entry = next(e for e in entries if e["principal_username"] == "api_user")

        self.assertEqual(mcp_entry["source"], "ai_assistant")
        self.assertIsNone(api_entry["source"])

    def test_list_audit_logs_with_authorization_includes_source(self):
        """list_audit_logs with include_authorization also includes source field."""
        AuditLog.objects.create(
            principal_username="mcp_user",
            resource_type=AuditLog.GROUP,
            action=AuditLog.CREATE,
            description="Created group: Auth Test",
            source=AuditLog.SOURCE_AI_ASSISTANT,
            tenant=self.tenant,
        )

        response = self._call_tool("list_audit_logs", {"include_authorization": True})

        self.assertEqual(response.status_code, 200)
        tool_output = self._get_tool_output(response)
        entries = tool_output["data"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source"], "ai_assistant")
        self.assertIn("authorized_by", entries[0])


class MCPAccessCheckTests(MCPToolTestMixin, IdentityRequest):
    """Test the dual-path (v1/v2) permission check for MCP tools."""

    def setUp(self):
        """Set up the access check tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)

    def tearDown(self):
        """Tear down access check tests."""
        AuditLog.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    # --- ToolConfig ---

    def test_tool_config_has_permission_fields(self):
        """ToolConfig includes required_relation, required_resource_type, v1_permission, and redacted_fields."""
        config = ToolConfig(fn=lambda: "")
        self.assertIsNone(config.required_relation)
        self.assertEqual(config.required_resource_type, "tenant")
        self.assertIsNone(config.v1_permission)
        self.assertEqual(config.redacted_fields, ())

    def test_tool_config_with_permission_fields(self):
        """ToolConfig accepts all permission fields."""
        config = ToolConfig(
            fn=lambda: "",
            required_relation="rbac_roles_read",
            required_resource_type="workspace",
            v1_permission=("role", "read"),
        )
        self.assertEqual(config.required_relation, "rbac_roles_read")
        self.assertEqual(config.required_resource_type, "workspace")
        self.assertEqual(config.v1_permission, ("role", "read"))

    def test_protected_tools_have_required_relation(self):
        """All protected tools have required_relation set."""
        expected_protected = [
            "list_audit_logs",
            "investigate_tam_access",
            "audit_redhat_access",
            "audit_group_for_dissolution",
            "get_user_state",
            "get_rbac_recent_changes",
            "investigate_group_changes",
            "guide_user_access_delegation",
            "search_roles",
            "get_role",
            "check_role_permissions",
            "investigate_user_access",
            "check_user_permission",
        ]
        for tool_name in expected_protected:
            config = _TOOL_CONFIG.get(tool_name)
            self.assertIsNotNone(config, f"Tool '{tool_name}' not found in _TOOL_CONFIG")
            self.assertIsNotNone(
                config.required_relation,
                f"Tool '{tool_name}' missing required_relation",
            )

    def test_protected_tools_have_v1_permission(self):
        """All protected tools have v1_permission set for v1 org fallback."""
        expected_v1_permissions = {
            "list_audit_logs": ("admin", "only"),
            "get_rbac_recent_changes": ("admin", "only"),
            "search_roles": ("role", "read"),
            "get_role": ("role", "read"),
            "check_role_permissions": ("role", "read"),
            "investigate_tam_access": ("role", "read"),
            "audit_redhat_access": ("role", "read"),
            "guide_user_access_delegation": ("role", "read"),
            "audit_group_for_dissolution": ("group", "read"),
            "investigate_group_changes": ("group", "read"),
            "check_user_permission": ("principal", "read"),
            "get_user_state": ("principal", "read"),
            "investigate_user_access": ("principal", "read"),
        }
        for tool_name, expected_perm in expected_v1_permissions.items():
            config = _TOOL_CONFIG.get(tool_name)
            self.assertIsNotNone(config, f"Tool '{tool_name}' not found in _TOOL_CONFIG")
            self.assertEqual(
                config.v1_permission,
                expected_perm,
                f"Tool '{tool_name}' has wrong v1_permission: {config.v1_permission}",
            )

    def test_principal_tools_have_redacted_fields(self):
        """Tools handling principal data declare redacted_fields."""
        expected_redacted = [
            "list_principals",
            "list_group_principals",
            "investigate_tam_access",
            "audit_redhat_access",
            "audit_group_for_dissolution",
            "get_user_state",
        ]
        for tool_name in expected_redacted:
            config = _TOOL_CONFIG.get(tool_name)
            self.assertIsNotNone(config, f"Tool '{tool_name}' not found in _TOOL_CONFIG")
            self.assertTrue(
                len(config.redacted_fields) > 0,
                f"Tool '{tool_name}' should declare redacted_fields",
            )
            self.assertIn("email", config.redacted_fields, f"Tool '{tool_name}' should redact email")
            self.assertIn("last_name", config.redacted_fields, f"Tool '{tool_name}' should redact last_name")

    def test_unprotected_tools_have_no_required_relation(self):
        """View-delegated tools do not have required_relation set."""
        unprotected = ["hello", "list_principals", "list_permissions", "list_groups", "get_group"]
        for tool_name in unprotected:
            config = _TOOL_CONFIG.get(tool_name)
            if config:
                self.assertIsNone(
                    config.required_relation,
                    f"Tool '{tool_name}' should not have required_relation",
                )

    # --- _check_kessel_access unit tests ---

    def test_check_kessel_access_no_tenant(self):
        """_check_kessel_access returns False when request has no tenant."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        self.assertFalse(_check_kessel_access(request, "tenant", "rbac_roles_read"))

    def test_check_kessel_access_no_resource_id(self):
        """_check_kessel_access returns False when tenant has no resource ID."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.tenant = self.tenant
        with patch.object(type(self.tenant), "tenant_resource_id", return_value=None):
            self.assertFalse(_check_kessel_access(request, "tenant", "rbac_roles_read"))

    @patch("management.mcp_views.get_kessel_principal_id", return_value=None)
    def test_check_kessel_access_no_principal_id(self, _mock_principal):
        """_check_kessel_access returns False when principal ID cannot be resolved."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.tenant = self.tenant
        self.assertFalse(_check_kessel_access(request, "tenant", "rbac_roles_read"))

    @patch("management.mcp_views.get_kessel_principal_id", return_value="localhost/test_user")
    @patch("management.mcp_views.WorkspaceInventoryAccessChecker.check_resource_access", return_value=True)
    def test_check_kessel_access_granted(self, mock_checker, _mock_principal):
        """_check_kessel_access returns True when Kessel grants access."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.tenant = self.tenant
        self.assertTrue(_check_kessel_access(request, "tenant", "rbac_roles_read"))
        mock_checker.assert_called_once_with(
            resource_type="tenant",
            resource_id=self.tenant.tenant_resource_id(),
            principal_id="localhost/test_user",
            relation="rbac_roles_read",
        )

    @patch("management.mcp_views.get_kessel_principal_id", return_value="localhost/test_user")
    @patch("management.mcp_views.WorkspaceInventoryAccessChecker.check_resource_access", return_value=False)
    def test_check_kessel_access_denied(self, mock_checker, _mock_principal):
        """_check_kessel_access returns False when Kessel denies access."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.tenant = self.tenant
        self.assertFalse(_check_kessel_access(request, "tenant", "rbac_roles_read"))

    @patch("management.mcp_views.get_kessel_principal_id", side_effect=Exception("Dependency error"))
    def test_check_kessel_access_principal_exception_fails_closed(self, _mock_principal):
        """_check_kessel_access returns False when get_kessel_principal_id raises (fail-closed)."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.tenant = self.tenant
        self.assertFalse(_check_kessel_access(request, "tenant", "rbac_roles_read"))

    @patch("management.mcp_views.get_kessel_principal_id", return_value="localhost/test_user")
    @patch(
        "management.mcp_views.WorkspaceInventoryAccessChecker.check_resource_access",
        side_effect=Exception("Inventory API error"),
    )
    def test_check_kessel_access_inventory_exception_fails_closed(self, _mock_checker, _mock_principal):
        """_check_kessel_access returns False when check_resource_access raises (fail-closed)."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.tenant = self.tenant
        self.assertFalse(_check_kessel_access(request, "tenant", "rbac_roles_read"))

    # --- _check_v1_access unit tests ---

    def test_check_v1_access_admin_user(self):
        """_check_v1_access returns True for org admin regardless of permission type."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = type("User", (), {"admin": True, "access": {}})()
        self.assertTrue(_check_v1_access(request, ("role", "read")))
        self.assertTrue(_check_v1_access(request, ("admin", "only")))

    def test_check_v1_access_admin_only_non_admin(self):
        """_check_v1_access returns False for non-admin when admin-only is required."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = type("User", (), {"admin": False, "access": {"role": {"read": ["*"]}}})()
        self.assertFalse(_check_v1_access(request, ("admin", "only")))

    def test_check_v1_access_role_read_granted(self):
        """_check_v1_access returns True when user has role:read access."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = type("User", (), {"admin": False, "access": {"role": {"read": ["*"]}}})()
        self.assertTrue(_check_v1_access(request, ("role", "read")))

    def test_check_v1_access_group_read_granted(self):
        """_check_v1_access returns True when user has group:read access."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = type("User", (), {"admin": False, "access": {"group": {"read": ["*"]}}})()
        self.assertTrue(_check_v1_access(request, ("group", "read")))

    def test_check_v1_access_principal_read_granted(self):
        """_check_v1_access returns True when user has principal:read access."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = type("User", (), {"admin": False, "access": {"principal": {"read": ["*"]}}})()
        self.assertTrue(_check_v1_access(request, ("principal", "read")))

    def test_check_v1_access_no_matching_access(self):
        """_check_v1_access returns False when user lacks required access."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = type("User", (), {"admin": False, "access": {"group": {"read": ["*"]}}})()
        self.assertFalse(_check_v1_access(request, ("role", "read")))

    def test_check_v1_access_empty_access(self):
        """_check_v1_access returns False when user has empty access dict."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = type("User", (), {"admin": False, "access": {}})()
        self.assertFalse(_check_v1_access(request, ("role", "read")))

    def test_check_v1_access_no_user(self):
        """_check_v1_access returns False when request has no user."""
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        self.assertFalse(_check_v1_access(request, ("role", "read")))

    # --- Integration: dual-path dispatch ---

    @patch("management.mcp_views.is_v2_write_activated", return_value=True)
    @patch("management.mcp_views._check_kessel_access", return_value=False)
    def test_v2_org_uses_kessel_check(self, mock_kessel, _mock_v2):
        """V2 org dispatches to Kessel check; denied when Kessel returns False."""
        response = self._call_tool("list_audit_logs")

        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32003)
        mock_kessel.assert_called_once()

    @patch("management.mcp_views.is_v2_write_activated", return_value=True)
    @patch("management.mcp_views._check_kessel_access", return_value=True)
    def test_v2_org_kessel_granted(self, _mock_kessel, _mock_v2):
        """V2 org dispatches to Kessel check; allowed when Kessel returns True."""
        response = self._call_tool("list_audit_logs")

        data = response.json()
        self.assertIn("result", data)
        self.assertFalse(data["result"]["isError"])

    @patch("management.mcp_views.is_v2_write_activated", return_value=False)
    @patch("management.mcp_views._check_v1_access", return_value=False)
    def test_v1_org_uses_v1_check(self, mock_v1, _mock_v2):
        """V1 org dispatches to v1 access check; denied when v1 returns False."""
        response = self._call_tool("search_roles")

        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32003)
        mock_v1.assert_called_once()

    @patch("management.mcp_views.is_v2_write_activated", return_value=False)
    @patch("management.mcp_views._check_v1_access", return_value=True)
    def test_v1_org_v1_access_granted(self, _mock_v1, _mock_v2):
        """V1 org dispatches to v1 access check; allowed when v1 returns True."""
        response = self._call_tool("search_roles")

        data = response.json()
        self.assertIn("result", data)
        self.assertFalse(data["result"]["isError"])

    @patch("management.mcp_views.is_v2_write_activated", return_value=False)
    @patch("management.mcp_views._check_v1_access", return_value=False)
    def test_v1_org_admin_only_tool_denied(self, mock_v1, _mock_v2):
        """V1 org with admin-only tool (list_audit_logs) denied for non-admin."""
        response = self._call_tool("list_audit_logs")

        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32003)

    @patch("management.mcp_views.is_v2_write_activated", return_value=False)
    @patch("management.mcp_views._check_v1_access", return_value=True)
    def test_v1_org_admin_only_tool_granted(self, _mock_v1, _mock_v2):
        """V1 org with admin-only tool (list_audit_logs) granted for admin."""
        response = self._call_tool("list_audit_logs")

        data = response.json()
        self.assertIn("result", data)
        self.assertFalse(data["result"]["isError"])

    def test_tool_without_required_relation_skips_both_checks(self):
        """Tools without required_relation do not trigger any permission check."""
        with (
            patch("management.mcp_views._check_kessel_access") as mock_kessel,
            patch("management.mcp_views._check_v1_access") as mock_v1,
        ):
            self._call_tool("hello", {"message": "test"})
            mock_kessel.assert_not_called()
            mock_v1.assert_not_called()

    @patch("management.mcp_views._check_kessel_access", return_value=False)
    @patch("management.mcp_views._check_v1_access", return_value=False)
    def test_auth_check_runs_before_permission_check(self, _mock_v1, _mock_kessel):
        """Auth check (org_id) runs before permission check — unauthenticated gets -32000."""
        response = self._call_tool("list_audit_logs", use_auth=False)

        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32000)
        _mock_kessel.assert_not_called()
        _mock_v1.assert_not_called()


class MCPWriteConfirmationSettingGuardTests(IdentityRequest):
    """Guard: every MCPToolTestMixin subclass with MCP_WRITE_ENABLED must declare MCP_WRITE_CONFIRMATION."""

    def test_all_write_enabled_classes_declare_confirmation_setting(self):
        """Prevent regressions where a write-enabled test class forgets MCP_WRITE_CONFIRMATION."""
        module = inspect.getmodule(MCPToolTestMixin)
        missing = []
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if not issubclass(cls, MCPToolTestMixin) or cls is MCPToolTestMixin:
                continue
            wrapper = getattr(cls, "cls_atomics", None)
            settings_override = None
            for decorator_data in cls.__dict__.values():
                if hasattr(decorator_data, "__wrapped__"):
                    break
            overrides = getattr(cls, "_overridden_settings", None)
            if overrides is None:
                continue
            if not overrides.get("MCP_WRITE_ENABLED", False):
                continue
            if "MCP_WRITE_CONFIRMATION" not in overrides:
                missing.append(name)
        self.assertEqual(
            missing,
            [],
            f"These test classes have MCP_WRITE_ENABLED=True but do not explicitly set "
            f"MCP_WRITE_CONFIRMATION (True for confirmation tests, False for others): {missing}",
        )


@override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=True)
class MCPWriteConfirmationTests(MCPToolTestMixin, IdentityRequest):
    """Test the two-phase write confirmation system."""

    def setUp(self):
        """Set up write confirmation tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)

    def tearDown(self):
        """Tear down write confirmation tests."""
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    def _get_confirmation_token(self, tool_name, arguments):
        """Call a write tool and extract the confirmation token from the response."""
        response = self._call_tool(tool_name, arguments)
        output = json.loads(response.json()["result"]["content"][0]["text"])
        return output["confirmation_token"]

    def test_write_tool_returns_confirmation_required(self):
        """Write tool without confirmation_token returns confirmation prompt."""
        response = self._call_tool("create_group", {"name": "Test Group"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("result", data)
        self.assertFalse(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertTrue(output["confirmation_required"])
        self.assertIn("confirmation_token", output)
        self.assertIn("create a new group 'Test Group'", output["message"])

    def test_write_tool_executes_with_valid_token(self):
        """Write tool with valid confirmation_token executes the operation."""
        token = self._get_confirmation_token("create_group", {"name": "Confirmed Group"})

        response2 = self._call_tool("create_group", {"name": "Confirmed Group", "confirmation_token": token})

        self.assertEqual(response2.status_code, 200)
        data = response2.json()
        self.assertFalse(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output["name"], "Confirmed Group")

    def test_invalid_token_rejected(self):
        """Invalid confirmation token is rejected."""
        response = self._call_tool("create_group", {"name": "Test Group", "confirmation_token": "invalid-token-value"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertIn("invalid or expired", output["error"])

    def test_token_cannot_be_reused(self):
        """Confirmation token can only be used once."""
        token = self._get_confirmation_token("create_group", {"name": "One Time Group"})

        self._call_tool("create_group", {"name": "One Time Group", "confirmation_token": token})

        response3 = self._call_tool("create_group", {"name": "One Time Group", "confirmation_token": token})

        data = response3.json()
        self.assertTrue(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertIn("invalid or expired", output["error"])

    def test_token_rejected_for_different_arguments(self):
        """Token issued for one set of arguments cannot be used with different arguments."""
        token = self._get_confirmation_token("create_group", {"name": "Group A"})

        response2 = self._call_tool(
            "create_group",
            {"name": "Group B", "confirmation_token": token},
        )

        data = response2.json()
        self.assertTrue(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertIn("changed since the confirmation", output["error"])

    def test_token_rejected_for_different_tool(self):
        """Token issued for one tool cannot be used for a different tool."""
        token = self._get_confirmation_token("create_group", {"name": "Cross Tool Test"})

        response2 = self._call_tool(
            "update_group",
            {"group_name": "Cross Tool Test", "name": "New Name", "confirmation_token": token},
        )

        data = response2.json()
        self.assertTrue(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertIn("issued for 'create_group'", output["error"])

    def test_read_tools_bypass_confirmation(self):
        """Read-only tools execute without confirmation."""
        response = self._call_tool("list_groups")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertNotIn("confirmation_required", output)

    def test_delete_tool_confirmation_shows_destructive(self):
        """Delete tools show DESTRUCTIVE warning in confirmation message."""
        group = Group.objects.create(name="Delete Me", tenant=self.tenant)

        response = self._call_tool("delete_group", {"group_uuid": str(group.uuid)})

        data = response.json()
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertTrue(output["confirmation_required"])
        self.assertIn("DESTRUCTIVE", output["message"])
        self.assertIn("irreversible", output["message"])

    def test_confirmation_disabled_bypasses_check(self):
        """When MCP_WRITE_CONFIRMATION=False, write tools execute immediately."""
        with self.settings(MCP_WRITE_CONFIRMATION=False):
            response = self._call_tool("create_group", {"name": "Direct Group"})

            data = response.json()
            self.assertFalse(data["result"]["isError"])
            output = json.loads(data["result"]["content"][0]["text"])
            self.assertEqual(output["name"], "Direct Group")

    @override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=True)
    def test_initialize_includes_confirmation_instructions(self):
        """Initialize response includes confirmation protocol instructions when enabled."""
        body = {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {"clientInfo": {"name": "test"}}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        data = response.json()
        instructions = data["result"]["instructions"]
        self.assertIn("Write Confirmation Protocol", instructions)
        self.assertIn("confirmation_token", instructions)

    @override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False)
    def test_initialize_omits_confirmation_instructions_when_disabled(self):
        """Initialize response omits confirmation instructions when disabled."""
        body = {"jsonrpc": "2.0", "method": "initialize", "id": 1, "params": {"clientInfo": {"name": "test"}}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        data = response.json()
        instructions = data["result"]["instructions"]
        self.assertNotIn("Write Confirmation Protocol", instructions)

    @override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=True)
    def test_write_tool_descriptions_include_confirmation_suffix(self):
        """Write tool descriptions include confirmation protocol instructions."""
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 1, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        data = response.json()
        tools = {t["name"]: t for t in data["result"]["tools"]}
        create_group = tools["create_group"]
        self.assertIn("Write Confirmation", create_group["description"])
        self.assertIn("NEVER auto-confirm", create_group["description"])

    @override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=False)
    def test_write_tool_descriptions_omit_confirmation_when_disabled(self):
        """Write tool descriptions omit confirmation suffix when disabled."""
        body = {"jsonrpc": "2.0", "method": "tools/list", "id": 1, "params": {}}
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)

        data = response.json()
        tools = {t["name"]: t for t in data["result"]["tools"]}
        create_group = tools["create_group"]
        self.assertNotIn("Write Confirmation", create_group["description"])

    def test_confirmation_token_not_passed_to_tool_function(self):
        """The confirmation_token argument is stripped before calling the tool function."""
        import dataclasses
        from unittest.mock import MagicMock

        token = self._get_confirmation_token("create_group", {"name": "Token Strip Test"})
        original_config = _TOOL_CONFIG["create_group"]
        mock_fn = MagicMock(wraps=original_config.fn)
        mock_config = dataclasses.replace(original_config, fn=mock_fn)

        with patch.dict("management.mcp_views._TOOL_CONFIG", {"create_group": mock_config}):
            response2 = self._call_tool("create_group", {"name": "Token Strip Test", "confirmation_token": token})

        data = response2.json()
        self.assertFalse(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertEqual(output["name"], "Token Strip Test")
        mock_fn.assert_called_once()
        _, kwargs = mock_fn.call_args
        self.assertNotIn("confirmation_token", kwargs)

    def test_token_rejected_for_different_org(self):
        """Token issued for one org cannot be used by a different org."""
        from base64 import b64encode
        from json import dumps as json_dumps

        token = self._get_confirmation_token("create_group", {"name": "Cross Org Test"})

        other_customer = self._create_customer_data()
        other_tenant = Tenant.objects.create(
            tenant_name=other_customer["tenant_name"],
            account_id=other_customer["account_id"],
            org_id=other_customer["org_id"],
            ready=True,
        )
        other_identity = self._build_identity(
            self.user_data, other_customer["account_id"], other_customer["org_id"], True, False
        )
        other_header = b64encode(json_dumps(other_identity).encode("utf-8"))
        other_headers = {"HTTP_X_RH_IDENTITY": other_header}

        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 1,
            "params": {
                "name": "create_group",
                "arguments": {"name": "Cross Org Test", "confirmation_token": token},
            },
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **other_headers)

        data = response.json()
        self.assertTrue(data["result"]["isError"])
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertIn("different organization", output["error"])

        other_tenant.delete()


@override_settings(MCP_WRITE_ENABLED=True, MCP_WRITE_CONFIRMATION=True)
class MCPWritePreviewTests(MCPToolTestMixin, IdentityRequest):
    """Test preview message generation for different write tool categories."""

    def setUp(self):
        """Set up preview tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)

    def tearDown(self):
        """Tear down preview tests."""
        Group.objects.all().delete()
        Principal.objects.all().delete()
        super().tearDown()

    def _get_preview_message(self, tool_name, arguments):
        """Call a tool and extract the confirmation preview message."""
        response = self._call_tool(tool_name, arguments)
        output = json.loads(response.json()["result"]["content"][0]["text"])
        return output["message"]

    def test_create_group_preview(self):
        """create_group shows group name in preview."""
        msg = self._get_preview_message("create_group", {"name": "Engineering Team"})
        self.assertIn("create a new group 'Engineering Team'", msg)

    def test_add_principals_preview(self):
        """add_principals_to_group shows principal count in preview."""
        group = Group.objects.create(name="Team", tenant=self.tenant)
        msg = self._get_preview_message(
            "add_principals_to_group",
            {"group_uuid": str(group.uuid), "principals": ["user1", "user2"]},
        )
        self.assertIn("2 principal(s)", msg)

    def test_delete_group_preview(self):
        """delete_group shows DESTRUCTIVE and group identifier in preview."""
        group = Group.objects.create(name="Old Team", tenant=self.tenant)
        msg = self._get_preview_message("delete_group", {"group_name": "Old Team"})
        self.assertIn("DESTRUCTIVE", msg)
        self.assertIn("Old Team", msg)

    def test_update_group_preview(self):
        """update_group shows old and new name in preview."""
        group = Group.objects.create(name="Dev Team", tenant=self.tenant)
        msg = self._get_preview_message(
            "update_group",
            {"group_name": "Dev Team", "name": "Platform Team"},
        )
        self.assertIn("update group 'Dev Team'", msg)
        self.assertIn("Platform Team", msg)


class MCPSchemaValidationTests(MCPToolTestMixin, IdentityRequest):
    """Test JSON schema pre-validation of MCP tool arguments."""

    def setUp(self):
        """Set up schema validation tests."""
        super().setUp()
        self.url = "/_private/_a2s/mcp/"
        self.client = APIClient()
        self.principal = Principal.objects.create(username="test_user", tenant=self.tenant)

    def tearDown(self):
        """Tear down schema validation tests."""
        Principal.objects.all().delete()
        super().tearDown()

    def _call_tool_raw(self, tool_name: str, arguments: dict) -> dict:
        """Call tool and return the parsed JSON response."""
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "id": 200,
            "params": {"name": tool_name, "arguments": arguments},
        }
        response = self.client.post(self.url, data=json.dumps(body), content_type="application/json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    # --- Wrong type tests ---

    def test_wrong_type_string_as_integer(self):
        """Schema validation rejects string where integer is expected."""
        data = self._call_tool_raw("hello", {"message": 123})
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32602)
        self.assertIn("Expected", data["error"]["message"])
        self.assertIn("string", data["error"]["message"])
        self.assertNotIn("TypeError", data["error"]["message"])

    def test_wrong_type_does_not_leak_internal_names(self):
        """Schema validation error does not expose Python exception details."""
        data = self._call_tool_raw("hello", {"message": ["not", "a", "string"]})
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32602)
        self.assertNotIn("TypeError", data["error"]["message"])
        self.assertNotIn("unexpected keyword", data["error"]["message"])
        self.assertNotIn("traceback", data["error"]["message"].lower())

    # --- Extra unknown arguments ---

    def test_extra_unknown_argument_rejected(self):
        """Schema validation rejects arguments not in the tool schema."""
        data = self._call_tool_raw("hello", {"message": "hi", "unknown_param": "x"})
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32602)
        self.assertIn("Unknown argument", data["error"]["message"])

    def test_multiple_extra_arguments_rejected(self):
        """Schema validation rejects even when valid args are mixed with extra ones."""
        data = self._call_tool_raw("hello", {"message": "hi", "foo": 1, "bar": 2})
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32602)

    # --- Missing required arguments ---

    def test_missing_required_argument_rejected(self):
        """Schema validation rejects calls missing required arguments."""
        data = self._call_tool_raw("get_role", {})
        self.assertIn("error", data)
        self.assertEqual(data["error"]["code"], -32602)
        self.assertIn("Missing required argument", data["error"]["message"])

    # --- Valid arguments pass through ---

    def test_valid_arguments_pass_validation(self):
        """Schema validation allows valid arguments through to tool execution."""
        data = self._call_tool_raw("hello", {"message": "Hi there!"})
        self.assertIn("result", data)
        self.assertNotIn("error", data)
        output = json.loads(data["result"]["content"][0]["text"])
        self.assertIn("Hi there!", output["response"])

    def test_valid_arguments_with_defaults_omitted(self):
        """Schema validation passes when optional arguments are omitted."""
        data = self._call_tool_raw("hello", {})
        self.assertIn("result", data)
        self.assertNotIn("error", data)

    # --- Unit tests for _sanitize_validation_error ---

    def test_sanitize_type_error(self):
        """_sanitize_validation_error formats type errors cleanly."""
        import jsonschema as js

        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        try:
            js.validate({"name": 42}, schema)
        except js.ValidationError as exc:
            msg = _sanitize_validation_error(exc)
            self.assertIn("Expected", msg)
            self.assertIn("string", msg)
            self.assertIn("int", msg)

    def test_sanitize_required_error(self):
        """_sanitize_validation_error formats required errors cleanly."""
        import jsonschema as js

        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        try:
            js.validate({}, schema)
        except js.ValidationError as exc:
            msg = _sanitize_validation_error(exc)
            self.assertEqual(msg, "Missing required argument: name")

    def test_sanitize_additional_properties_error(self):
        """_sanitize_validation_error formats additionalProperties errors cleanly."""
        import jsonschema as js

        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "additionalProperties": False}
        try:
            js.validate({"name": "ok", "extra": "bad"}, schema)
        except js.ValidationError as exc:
            msg = _sanitize_validation_error(exc)
            self.assertIn("Unknown argument", msg)

    # --- Unit test for _validate_tool_arguments ---

    def test_validate_unknown_tool_returns_none(self):
        """_validate_tool_arguments returns None for tools with no schema."""
        result = _validate_tool_arguments("nonexistent_tool_xyz", {"anything": True})
        self.assertIsNone(result)

    def test_validate_valid_returns_none(self):
        """_validate_tool_arguments returns None for valid arguments."""
        result = _validate_tool_arguments("hello", {"message": "test"})
        self.assertIsNone(result)

    def test_validate_invalid_returns_message(self):
        """_validate_tool_arguments returns error message for invalid arguments."""
        result = _validate_tool_arguments("hello", {"message": 999})
        self.assertIsNotNone(result)
        self.assertIn("string", result)
