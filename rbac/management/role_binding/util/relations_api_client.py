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
"""Client for the Kessel Relations API for role binding lookups."""

import logging
import uuid
from typing import Optional

from django.conf import settings
from google.protobuf import json_format
from internal.jwt_utils import JWTManager, JWTProvider
from kessel.relations.v1beta1 import common_pb2, lookup_pb2, lookup_pb2_grpc
from management.cache import JWTCache
from management.utils import create_client_channel_relation

logger = logging.getLogger(__name__)

# Module-level JWT manager singleton
_jwt_cache = JWTCache()
_jwt_provider = JWTProvider()
_jwt_manager = JWTManager(_jwt_provider, _jwt_cache)


def parse_resource_type(resource_type: str) -> tuple[str, str]:
    """Parse a resource type string into namespace and name.

    Args:
        resource_type: Resource type, optionally prefixed with namespace
                      (e.g., "rbac/workspace" or "workspace")

    Returns:
        Tuple of (namespace, name)
    """
    if "/" in resource_type:
        parts = resource_type.split("/", 1)
        return (parts[0], parts[1])
    return ("rbac", resource_type)


def lookup_binding_subjects(
    resource_type: str,
    resource_id: str,
    relation: str = "binding",
    subject_namespace: str = "rbac",
    subject_name: str = "role_binding",
) -> Optional[list[str]]:
    """Look up role_binding subjects related to a resource via the Relations API.

    This function finds all role_binding subjects that are related to the
    specified resource through the given relation. Use the recursive "binding"
    relation to find bindings on the resource and any parent resources.

    Args:
        resource_type: The resource type (e.g., "workspace" or "rbac/workspace")
        resource_id: The resource ID
        relation: The relation to traverse. Defaults to "binding" which recursively
                 finds bindings through the workspace hierarchy.
        subject_namespace: Namespace of the subject type. Defaults to "rbac".
        subject_name: Name of the subject type. Defaults to "role_binding".

    Returns:
        List of subject IDs found, or None if the lookup fails or is not configured.
    """
    if not settings.RELATION_API_SERVER:
        logger.warning("RELATION_API_SERVER is not configured; skipping relations lookup.")
        return None

    try:
        resource_ns, resource_name = parse_resource_type(resource_type)

        logger.info(
            "Looking up subjects: resource=%s/%s:%s, relation=%s, subject_type=%s/%s",
            resource_ns,
            resource_name,
            resource_id,
            relation,
            subject_namespace,
            subject_name,
        )

        token = _jwt_manager.get_jwt_from_redis()
        metadata = [("authorization", f"Bearer {token}")] if token else []
        subject_ids: set[str] = set()

        with create_client_channel_relation(settings.RELATION_API_SERVER) as channel:
            stub = lookup_pb2_grpc.KesselLookupServiceStub(channel)

            request = lookup_pb2.LookupSubjectsRequest(
                resource=common_pb2.ObjectReference(
                    type=common_pb2.ObjectType(namespace=resource_ns, name=resource_name),
                    id=str(resource_id),
                ),
                relation=relation,
                subject_type=common_pb2.ObjectType(namespace=subject_namespace, name=subject_name),
            )
            logger.debug("LookupSubjects request: %s", request)

            responses = stub.LookupSubjects(request, metadata=metadata)
            for idx, response in enumerate(responses, start=1):
                payload = json_format.MessageToDict(response)
                logger.debug("LookupSubjects response #%d: %s", idx, payload)

                # Handle different response formats
                subject = payload.get("subject", {})
                subject_id = subject.get("id") or subject.get("subject", {}).get("id")
                if subject_id:
                    try:
                        # NOTE: This is a temporary fix for an issue where LookupSubjects response from
                        # Relations API includes IDs used for hierarchy traversal (e.g., tenant_id).
                        # We filter them out by validating each ID is a valid UUID.
                        uuid.UUID(subject_id)
                        subject_ids.add(subject_id)
                    except ValueError:
                        logger.warning("Skipping non-UUID subject_id from Relations API: %s", subject_id)

        result = list(subject_ids)
        logger.info(
            "Found %d subject(s) for resource=%s/%s:%s, relation=%s: %s",
            len(result),
            resource_ns,
            resource_name,
            resource_id,
            relation,
            result,
        )
        return result

    except Exception:
        logger.exception("Failed to lookup subjects through Relations API")
        return None
