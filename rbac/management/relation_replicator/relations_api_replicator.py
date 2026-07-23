#
# Copyright 2024 Red Hat, Inc.
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

"""RelationReplicator which writes to the Relations API."""

import json
import logging
from typing import Optional

import grpc
from django.conf import settings
from google.protobuf import json_format
from google.rpc import error_details_pb2
from grpc_status import rpc_status
from internal.jwt_utils import JWTManager, JWTProvider
from kessel.relations.v1beta1 import common_pb2, relation_tuples_pb2
from kessel.relations.v1beta1 import relation_tuples_pb2_grpc
from management.cache import JWTCacheOptimized
from management.relation_replicator.relation_replicator import (
    RelationReplicator,
    ReplicationEvent,
)
from management.utils import create_client_channel_relation

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

# Initialize JWT manager for token handling (using optimized cache for Kafka consumer)
jwt_cache = JWTCacheOptimized()
jwt_provider = JWTProvider()
jwt_manager = JWTManager(jwt_provider, jwt_cache)


def execute_grpc_call(operation_name, grpc_callable, fencing_check=None, log_context=None):
    """Execute a gRPC call with standardized error handling.

    Args:
        operation_name: Name of the operation for logging (e.g., "write relationships", "delete relationship")
        grpc_callable: Callable that performs the gRPC operation
        fencing_check: Optional FencingCheck protobuf for distributed locking
        log_context: Optional dict with additional context for error logging

    Returns:
        The response from the gRPC call

    Raises:
        grpc.RpcError: If the gRPC call fails
    """
    try:
        return grpc_callable()
    except grpc.RpcError as err:
        error = GRPCError(err)

        # Check for invalid fencing token (FAILED_PRECONDITION)
        if err.code() == grpc.StatusCode.FAILED_PRECONDITION:
            logger.error(
                f"Invalid fencing token during {operation_name} - partition reassigned. "
                f"Lock ID: {fencing_check.lock_id if fencing_check else 'N/A'}, "
                f"Token: {fencing_check.lock_token if fencing_check else 'N/A'}. "
                f"Relations API error: code={error.code}, reason={error.reason}, message={error.message}"
            )
        else:
            # Build error message with context
            error_msg = f"Failed to {operation_name}: " f"error code {error.code}, reason {error.reason}"

            if log_context:
                context_str = ", ".join(f"{k}: {v}" for k, v in log_context.items())
                error_msg += f", {context_str}"

            logger.error(error_msg)
        raise


class RelationsApiReplicator(RelationReplicator):
    """Replicates relations via the Relations API over gRPC."""

    def replicate(self, event: ReplicationEvent):
        """Replicate the given event to Kessel Relations via the gRPC API."""
        self.write_relationships(event.add)

    def acquire_lock(self, lock_id: str) -> str:
        """Acquire a lock token from the Relations API.

        Args:
            lock_id: Unique identifier for the lock (format: "consumer-group/partition")

        Returns:
            str: The lock token

        Raises:
            grpc.RpcError: If the lock acquisition fails
        """
        # Get JWT token for authentication
        token = jwt_manager.get_jwt_from_redis()
        metadata = [("authorization", f"Bearer {token}")] if token else []

        with create_client_channel_relation(settings.RELATION_API_SERVER) as channel:
            stub = relation_tuples_pb2_grpc.KesselTupleServiceStub(channel)

            request = relation_tuples_pb2.AcquireLockRequest(lock_id=lock_id)

            response = execute_grpc_call(
                operation_name=f"acquire lock token for {lock_id}",
                grpc_callable=lambda: stub.AcquireLock(request, metadata=metadata),
                fencing_check=None,
                log_context={"lock_id": lock_id},
            )

            logger.info(f"Successfully acquired lock token for {lock_id}: {response.lock_token}")
            return response.lock_token

    def write_relationships(self, relationships, fencing_check=None):
        """Write relationships to the Relations API.

        Args:
            relationships: List of relationship tuples to create
            fencing_check: Optional FencingCheck protobuf for distributed locking

        Returns:
            CreateTuplesResponse from the API

        Raises:
            grpc.RpcError: If the API call fails (including FAILED_PRECONDITION for invalid fencing token)
        """
        # Get JWT token for authentication
        token = jwt_manager.get_jwt_from_redis()
        metadata = [("authorization", f"Bearer {token}")] if token else []

        with create_client_channel_relation(settings.RELATION_API_SERVER) as channel:
            stub = relation_tuples_pb2_grpc.KesselTupleServiceStub(channel)

            # Build request with optional fencing check
            request_kwargs = {
                "upsert": True,
                "tuples": relationships,
            }

            if fencing_check is not None:
                request_kwargs["fencing_check"] = fencing_check

            request = relation_tuples_pb2.CreateTuplesRequest(**request_kwargs)

            return execute_grpc_call(
                operation_name="write relationships to the relation API server",
                grpc_callable=lambda: stub.CreateTuples(request, metadata=metadata),
                fencing_check=fencing_check,
                log_context={"relationships": relationships},
            )

    def delete_relationships(self, relationships, fencing_check=None):
        """Delete relationships using the new filter-based API.

        For each relationship, create a filter that matches it exactly and delete it.

        Args:
            relationships: List of relationship tuples to delete
            fencing_check: Optional FencingCheck protobuf for distributed locking

        Returns:
            DeleteTuplesResponse from the API (last response if multiple deletes)

        Raises:
            grpc.RpcError: If the API call fails (including FAILED_PRECONDITION for invalid fencing token)
        """
        # If no relationships to delete, return an empty response
        if not relationships:
            logger.debug("No relationships to delete, returning empty response")
            # Return a mock response with empty consistency token
            return type(
                "obj",
                (object,),
                {"consistency_token": type("obj", (object,), {"token": None})()},
            )()

        # Get JWT token for authentication
        token = jwt_manager.get_jwt_from_redis()
        metadata = [("authorization", f"Bearer {token}")] if token else []

        with create_client_channel_relation(settings.RELATION_API_SERVER) as channel:
            stub = relation_tuples_pb2_grpc.KesselTupleServiceStub(channel)

            # Delete each relationship individually using filters
            responses = []
            for relationship in relationships:
                # Create a filter that matches this specific relationship
                relation_filter = relation_tuples_pb2.RelationTupleFilter(
                    resource_namespace=relationship.resource.type.namespace,
                    resource_type=relationship.resource.type.name,
                    resource_id=relationship.resource.id,
                    relation=relationship.relation,
                    subject_filter=relation_tuples_pb2.SubjectFilter(
                        subject_namespace=relationship.subject.subject.type.namespace,
                        subject_type=relationship.subject.subject.type.name,
                        subject_id=relationship.subject.subject.id,
                        relation=relationship.subject.relation or "",
                    ),
                )

                # Build request with optional fencing check
                request_kwargs = {
                    "filter": relation_filter,
                }

                if fencing_check is not None:
                    request_kwargs["fencing_check"] = fencing_check

                request = relation_tuples_pb2.DeleteTuplesRequest(**request_kwargs)

                response = execute_grpc_call(
                    operation_name="delete relationship from the relation API server",
                    grpc_callable=lambda req=request: stub.DeleteTuples(req, metadata=metadata),
                    fencing_check=fencing_check,
                    log_context={"relationship": relationship},
                )
                responses.append(response)

            # Return the last response (for consistency token)
            return responses[-1] if responses else None

    def read_tuples(
        self,
        resource_type: str,
        resource_id: str = "",
        relation: str = "",
        subject_type: str = "",
        subject_id: str = "",
        subject_relation: Optional[str] = None,
        resource_namespace: Optional[str] = None,
        subject_namespace: Optional[str] = None,
        pagination_limit: Optional[int] = None,
        continuation_token: Optional[str] = None,
    ) -> list[dict]:
        """Read tuples from the Relations API.

        Args:
            resource_type: Type of the resource (e.g., "tenant", "workspace", "role_binding", "role")
            resource_id: ID of the resource (empty string for all)
            relation: Relation to filter by (empty string for all relations)
            subject_type: Type of the subject to filter by (empty string for all)
            subject_id: ID of the subject to filter by (empty string for all)
            subject_relation: Optional subject relation filter
            resource_namespace: Namespace for resource (default "rbac")
            subject_namespace: Namespace for subject (default "rbac")

        Returns:
            list[dict]: List of tuple dictionaries from Kessel

        Raises:
            grpc.RpcError: If the API call fails
        """
        if (pagination_limit is None) and (continuation_token is not None):
            raise TypeError("A pagination limit must be provided if a continuation token is.")

        # TODO: replace this check with (not resource_type and not subject_type) if Kessel gets fixed.
        if not resource_type or not subject_type:
            raise ValueError("Both resource_type and subject_type must be provided (due to a Kessel limitation)")

        if resource_namespace is None:
            resource_namespace = "rbac" if resource_type != "" else ""

        if subject_namespace is None:
            subject_namespace = "rbac" if subject_type != "" else ""

        # Get JWT token for authentication
        token = jwt_manager.get_jwt_from_redis()
        metadata = [("authorization", f"Bearer {token}")] if token else []

        with create_client_channel_relation(settings.RELATION_API_SERVER) as channel:
            stub = relation_tuples_pb2_grpc.KesselTupleServiceStub(channel)

            request = relation_tuples_pb2.ReadTuplesRequest(
                filter=relation_tuples_pb2.RelationTupleFilter(
                    resource_namespace=resource_namespace,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    relation=relation,
                    subject_filter=relation_tuples_pb2.SubjectFilter(
                        subject_namespace=subject_namespace,
                        subject_type=subject_type,
                        subject_id=subject_id,
                        relation=subject_relation,
                    ),
                ),
                pagination=(
                    common_pb2.RequestPagination(limit=pagination_limit, continuation_token=continuation_token)
                    if ((pagination_limit is not None) or (continuation_token is not None))
                    else None
                ),
            )

            responses = execute_grpc_call(
                operation_name="read tuples from the relation API server",
                grpc_callable=lambda: stub.ReadTuples(request, metadata=metadata),
                fencing_check=None,
                log_context={
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "relation": relation,
                },
            )

            result = []
            if responses:
                for r in responses:
                    result.append(json_format.MessageToDict(r))
            return result


class GRPCError:
    """A wrapper for a gRPC error."""

    code: grpc.StatusCode
    reason: str
    message: str
    metadata: dict

    def __init__(self, error: grpc.RpcError):
        """Initialize the error."""
        self.code = error.code()
        self.message = error.details()
        self.reason = "unknown"
        self.metadata = {}

        try:
            status = rpc_status.from_call(error)
            if status is not None and status.details:
                detail = status.details[0]
                info = error_details_pb2.ErrorInfo()
                detail.Unpack(info)
                self.reason = info.reason
                self.metadata = json.loads(str(info.metadata).replace("'", '"'))
        except Exception as e:
            logger.debug(f"Could not extract error details: {e}")
