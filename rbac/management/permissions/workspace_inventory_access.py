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

"""Workspace access checker using Inventory API."""

import logging
import time
from typing import Optional, Set

import grpc
from kessel.inventory.v1beta2 import allowed_pb2
from kessel.inventory.v1beta2.check_for_update_request_pb2 import CheckForUpdateRequest
from kessel.inventory.v1beta2.consistency_pb2 import Consistency
from kessel.inventory.v1beta2.consistency_token_pb2 import ConsistencyToken
from kessel.rbac.v2 import list_workspaces as sdk_list_workspaces
from management.inventory_client import (
    inventory_client,
    make_resource_ref,
    make_subject_ref,
)

from rbac import settings

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class WorkspaceInventoryAccessChecker:
    """Check workspace access using Inventory API."""

    # Used to calculate the max-items safety guard (PAGE_SIZE * MAX_PAGES).
    # The SDK handles actual gRPC pagination internally with its own limit.
    PAGE_SIZE = 1000
    # Used with PAGE_SIZE to cap total items fetched, preventing runaway iteration.
    MAX_PAGES = 10000

    def _log_and_return_allowed(
        self,
        allowed_value: int,
        workspace_id: str,
        principal_id: str,
        relation: str,
    ) -> bool:
        """
        Interpret the allowed status from an Inventory API response and log the result.

        Args:
            allowed_value: The allowed enum value from the protobuf response
            workspace_id: UUID of the workspace being checked
            principal_id: Principal identifier being checked
            relation: The relation being checked

        Returns:
            bool: True if access is granted, False otherwise
        """
        if allowed_value == allowed_pb2.Allowed.ALLOWED_TRUE:
            logger.debug(
                "Access granted: principal=%s, workspace=%s, relation=%s",
                principal_id,
                workspace_id,
                relation,
            )
            return True

        if allowed_value == allowed_pb2.Allowed.ALLOWED_FALSE:
            logger.debug(
                "Access denied: principal=%s, workspace=%s, relation=%s",
                principal_id,
                workspace_id,
                relation,
            )
            return False

        # Handle unexpected allowed status values
        logger.warning(
            "Unexpected allowed status from Inventory API: %s, workspace=%s, principal=%s, relation=%s",
            allowed_pb2.Allowed.Name(allowed_value),
            workspace_id,
            principal_id,
            relation,
        )
        return False

    def _call_inventory(self, rpc_fn, default):
        """
        Open stub, call rpc_fn(stub), catch connectivity errors, return default on failure.

        This helper centralizes gRPC stub setup and error handling. It handles connectivity
        and transport errors by returning a default value when the Inventory API is unreachable.
        It does NOT catch programming errors (AttributeError, TypeError, etc.) which should
        bubble up for debugging.

        Args:
            rpc_fn: Function that takes a stub and performs the RPC call
            default: The value to return if connectivity/transport errors occur

        Returns:
            Result of rpc_fn(stub) or default if connectivity errors occur
        """
        try:
            with inventory_client(settings.INVENTORY_API_SERVER) as stub:
                return rpc_fn(stub)
        except (ConnectionError, TimeoutError, grpc.RpcError) as e:
            # Network/connectivity/gRPC transport errors - log and return default
            logger.error(f"Inventory API connectivity error: {type(e).__name__}: {e}")
            return default

    def check_resource_access(
        self,
        resource_type: str,
        resource_id: str,
        principal_id: str,
        relation: str,
    ) -> bool:
        """
        Check if a principal has access to a specific resource using Inventory API CheckForUpdate.

        This method uses strongly consistent reads to ensure the most up-to-date permission
        state is used for all resource access checks.

        Args:
            resource_type: Type of resource to check (e.g., "workspace")
            resource_id: UUID of the resource to check
            principal_id: Principal identifier (e.g., "localhost/username")
            relation: The relation to check

        Returns:
            bool: True if principal has access, False otherwise
        """
        check_request = CheckForUpdateRequest(
            object=make_resource_ref(resource_type, resource_id),
            relation=relation,
            subject=make_subject_ref(principal_id),
        )

        def rpc(stub):
            response = stub.CheckForUpdate(check_request)
            return self._log_and_return_allowed(
                response.allowed,
                resource_id,
                principal_id,
                relation,
            )

        return self._call_inventory(rpc, False)

    def check_workspace_access(
        self,
        workspace_id: str,
        principal_id: str,
        relation: str,
    ) -> bool:
        """
        Check if a principal has access to a specific workspace using Inventory API CheckForUpdate.

        This is a convenience method that calls check_resource_access with resource_type="workspace".

        Args:
            workspace_id: UUID of the workspace to check
            principal_id: Principal identifier (e.g., "localhost/username")
            relation: The relation to check

        Returns:
            bool: True if principal has access, False otherwise
        """
        return self.check_resource_access(
            resource_type="workspace",
            resource_id=workspace_id,
            principal_id=principal_id,
            relation=relation,
        )

    def lookup_accessible_workspaces(
        self,
        principal_id: str,
        relation: str,
        request_id: Optional[str] = None,
        consistency_token: Optional[str] = None,
    ) -> Set[str]:
        """
        Lookup which workspaces are accessible to the principal via SDK list_workspaces.

        Uses the Kessel SDK's list_workspaces() function which handles gRPC request construction
        and continuation-token pagination internally. RBAC applies a max-items safety guard
        and timing instrumentation on top of the SDK call.

        Args:
            principal_id: Principal identifier (e.g., "localhost/username")
            relation: The relation to check
            request_id: Optional request ID for logging/tracing
            consistency_token: Optional consistency token for read-after-write correctness

        Returns:
            Set[str]: Set of workspace IDs that the principal has access to
        """

        def rpc(stub):
            logger.info(
                "lookup_accessible_workspaces called with consistency_token=%s",
                consistency_token,
            )

            subject_ref = make_subject_ref(principal_id)
            consistency = None
            if consistency_token:
                consistency = Consistency(
                    at_least_as_fresh=ConsistencyToken(token=consistency_token),
                )

            accessible_workspaces = set()
            item_count = 0
            max_items = self.PAGE_SIZE * self.MAX_PAGES

            t0 = time.perf_counter()

            for response in sdk_list_workspaces(
                inventory=stub,
                subject=subject_ref,
                relation=relation,
                consistency=consistency,
            ):
                workspace_id = getattr(getattr(response, "object", None), "resource_id", None)
                if workspace_id:
                    accessible_workspaces.add(workspace_id)
                else:
                    logger.warning(
                        "Malformed workspace response from SDK list_workspaces: "
                        "missing object.resource_id in response for principal=%s",
                        principal_id,
                    )

                item_count += 1
                if item_count >= max_items:
                    logger.warning(
                        "Reached maximum item limit (%d) while fetching workspaces "
                        "for principal=%s. Some workspaces may not be included.",
                        max_items,
                        principal_id,
                    )
                    break

            iteration_seconds = time.perf_counter() - t0

            logger.info(
                "Accessible workspaces for principal=%s: %d found via SDK list_workspaces",
                principal_id,
                len(accessible_workspaces),
            )

            if settings.WORKSPACE_ACCESS_TIMING_ENABLED:
                logger.info(
                    "lookup_accessible_workspaces timing: %s",
                    {
                        "request_id": request_id,
                        "principal_id": principal_id,
                        "relation": relation,
                        "workspace_count": len(accessible_workspaces),
                        "item_count": item_count,
                        "max_items": max_items,
                        "iteration_ms": round(iteration_seconds * 1000, 2),
                        "hit_max_items": item_count >= max_items,
                    },
                )

            return accessible_workspaces

        return self._call_inventory(rpc, set())
