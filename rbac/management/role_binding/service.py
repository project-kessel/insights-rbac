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
"""Service layer for role binding management."""

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

from django.conf import settings
from django.db import transaction
from django.db.models import CharField, Count, Exists, Max, Min, OuterRef, Prefetch, Q, QuerySet, TextChoices
from django.db.models.functions import Cast
from management.atomic_transactions import atomic
from management.exceptions import InvalidFieldError, NotFoundError, RequiredFieldError
from management.group.model import Group
from management.group.platform import DefaultGroupNotAvailableError, GlobalPolicyIdService
from management.permission.scope_service import (
    SCOPE_DISPLAY_NAME,
    Scope,
    default_implicit_resource_service,
    permission_scope_cache,
    resolve_workspace_scope,
    scope_for_resource,
)
from management.principal.model import Principal
from management.relation_replicator.noop_replicator import NoopReplicator
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.relation_replicator.relation_replicator import (
    PartitionKey,
    RelationReplicator,
    ReplicationEvent,
    ReplicationEventType,
)
from management.relation_replicator.types import RelationTuple
from management.role.platform import platform_v2_role_uuid_for
from management.role.v2_model import PlatformRoleV2, RoleV2
from management.role_binding.model import RoleBinding, RoleBindingGroup, RoleBindingPrincipal
from management.role_binding.util import lookup_binding_subjects
from management.subject import Subject, SubjectType
from management.tenant_mapping.model import DefaultAccessType, TenantMapping
from management.tenant_mapping.v2_activation import ensure_v2_write_activated
from management.workspace.model import Workspace

from api.models import Tenant


class ExcludeSources(TextChoices):
    """Enum for exclude_sources query parameter values."""

    DIRECT = "direct", "Exclude direct bindings"
    INDIRECT = "indirect", "Exclude inherited bindings"
    NONE = "none", "Show all bindings"


@dataclass
class UpdateRoleBindingResult:
    """Result of updating role bindings for a subject on a resource."""

    subject_type: str
    roles: list[RoleV2]
    resource_id: str
    resource_type: str
    subject: Group | Principal
    resource_name: Optional[str] = None
    custom_default_group_created: Optional[Group] = None


logger = logging.getLogger(__name__)


@dataclass
class CreateBindingRequest:
    """Typed input for a single role binding creation."""

    role_id: str
    resource_type: str
    resource_id: str
    subject_type: str
    subject_id: str


API_PRINCIPAL_SOURCE = "v2_api"


class RoleBindingService:
    """Service for role binding queries and operations."""

    def __init__(
        self,
        tenant: Tenant,
        replicator: RelationReplicator | None = None,
        principal_source: str = API_PRINCIPAL_SOURCE,
        allow_external_subjects: bool = False,
        skip_scope_validation: bool = False,
    ):
        """Initialize the service with a tenant and optional replicator."""
        self.tenant = tenant
        self._principal_source = principal_source
        self._allow_external_subjects = allow_external_subjects
        self._skip_scope_validation = skip_scope_validation

        if settings.REPLICATION_TO_RELATION_ENABLED:
            self._replicator = replicator if replicator is not None else OutboxReplicator()
        else:
            self._replicator = NoopReplicator()

    def _resolve_inherited_bindings(
        self,
        exclude_sources: ExcludeSources,
        resource_type: Optional[str],
        resource_id: Optional[str],
    ) -> tuple[Optional[list[str]], bool]:
        """Resolve inherited binding UUIDs based on exclude_sources parameter.

        Args:
            exclude_sources: ExcludeSources enum value controlling which bindings to include
            resource_type: The resource type (required for inherited lookups)
            resource_id: The resource ID (required for inherited lookups)

        Returns:
            Tuple of (binding_uuids, exclude_direct):
            - binding_uuids: List of inherited binding UUIDs, or None if not applicable
            - exclude_direct: True if direct bindings should be excluded
        """
        exclude_direct = exclude_sources == ExcludeSources.DIRECT
        include_inherited = exclude_sources in (ExcludeSources.DIRECT, ExcludeSources.NONE)

        binding_uuids = None
        if include_inherited and resource_type and resource_id:
            binding_uuids = self._lookup_binding_uuids_via_relations(resource_type, str(resource_id))

        return binding_uuids, exclude_direct

    def get_role_bindings_by_subject(self, params: dict) -> QuerySet:
        """Get role bindings grouped by subject from a dictionary of parameters.

        Args:
            params: Dictionary of validated query parameters (from input serializer)

        Returns:
            QuerySet of Group or Principal objects annotated with role binding information,
            depending on subject_type parameter.

        Note:
            Ordering is handled by V2CursorPagination.get_ordering() to ensure
            cursor pagination works correctly with the requested order_by parameter.
        """
        subject_type = params.get("subject_type")
        resource_id = params["resource_id"]
        resource_type = params["resource_type"]
        subject_id = params.get("subject_id")
        exclude_sources = params.get("exclude_sources", ExcludeSources.NONE)

        # Ensure default bindings exist (lazy creation)
        self._ensure_default_bindings_exist()

        binding_uuids, exclude_direct = self._resolve_inherited_bindings(exclude_sources, resource_type, resource_id)

        if subject_type == SubjectType.USER:
            # Build user queryset
            queryset = self._build_user_queryset(
                resource_id, resource_type, binding_uuids, exclude_direct=exclude_direct
            )
            queryset = self._apply_user_filters(queryset, subject_id)
        else:
            # Default to group queryset (includes when subject_type is None or "group")
            queryset = self._build_base_queryset(
                resource_id, resource_type, binding_uuids, exclude_direct=exclude_direct
            )
            queryset = self._apply_subject_filters(queryset, subject_type, subject_id)

        return queryset

    def get_role_bindings_for_list(self, params: dict) -> QuerySet:
        """Get role bindings for the list endpoint with inherited binding support.

        Args:
            params: Dictionary of validated query parameters (from input serializer)
                - role_id: Optional role UUID filter
                - resource_id: Optional resource ID filter
                - resource_type: Optional resource type filter
                - subject_type: Optional subject type filter
                - subject_id: Optional subject ID filter
                - exclude_sources: ExcludeSources enum value

        Returns:
            QuerySet of RoleBinding objects

        Note:
            Ordering is handled by V2CursorPagination.get_ordering() to ensure
            cursor pagination works correctly with the requested order_by parameter.
        """
        resource_id = params.get("resource_id")
        resource_type = params.get("resource_type")
        exclude_sources = params.get("exclude_sources", ExcludeSources.NONE)

        binding_uuids, exclude_direct = self._resolve_inherited_bindings(exclude_sources, resource_type, resource_id)

        # Handle edge case: exclude_direct but no inherited bindings available
        if exclude_direct and binding_uuids is None:
            # Relations API failed or not configured — cannot determine inherited bindings
            return RoleBinding.objects.for_tenant(self.tenant).none()

        queryset = RoleBinding.objects.for_tenant(self.tenant)

        # Apply role filter
        role_id = params.get("role_id")
        if role_id:
            queryset = queryset.for_role(role_id)

        # Apply subject filters
        queryset = queryset.for_subject(
            subject_type=params.get("subject_type"),
            subject_id=params.get("subject_id"),
        )

        # Build resource filter based on exclude_sources logic
        if exclude_direct and binding_uuids is not None:
            # Only inherited bindings by UUID (exclude direct)
            queryset = queryset.filter(
                Q(uuid__in=binding_uuids) & ~Q(resource_type=resource_type, resource_id=str(resource_id))
            )
        elif binding_uuids is not None and resource_id and resource_type:
            # Both direct and inherited bindings (exclude_sources=none)
            queryset = queryset.filter(
                Q(resource_type=resource_type, resource_id=str(resource_id)) | Q(uuid__in=binding_uuids)
            )
        else:
            # Only direct bindings (exclude_sources=indirect or no resource filter)
            queryset = queryset.for_resource_filter(
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id else None,
            )

        return queryset

    def get_resource_name(self, resource_id: str, resource_type: str) -> Optional[str]:
        """Get the name of a resource by ID and type.

        Args:
            resource_id: The resource identifier
            resource_type: The type of resource (e.g., 'workspace', 'tenant')

        Returns:
            Resource name or None if not found
        """
        if resource_type == "workspace":
            try:
                workspace = Workspace.objects.get(id=resource_id, tenant=self.tenant)
                return workspace.name
            except Workspace.DoesNotExist:
                logger.warning(f"Workspace {resource_id} not found for tenant {self.tenant}")
                return None
        if resource_type == "tenant" and resource_id == self.tenant.tenant_resource_id():
            return self.tenant.tenant_name
        return None

    def build_context(self, params: dict) -> dict:
        """Build serializer context with resource information from a dictionary.

        Args:
            params: Dictionary of validated query parameters (from input serializer).
                    The 'fields' key contains an already-parsed FieldSelection object or None.

        Returns:
            Context dict for output serializer
        """
        resource_id = params["resource_id"]
        resource_type = params["resource_type"]

        return {
            "resource_id": resource_id,
            "resource_type": resource_type,
            "resource_name": self.get_resource_name(resource_id, resource_type),
            "field_selection": params.get("fields"),
            "subject_type": params.get("subject_type"),
        }

    @atomic
    def batch_create(self, requests: list[CreateBindingRequest]) -> list[dict]:
        """Create multiple role bindings."""
        ensure_v2_write_activated(self.tenant)

        if len(requests) == 0:
            raise RequiredFieldError("requests")

        roles = self._get_roles(list({req.role_id for req in requests}))
        roles_by_uuid = {str(r.uuid): r for r in roles}
        roles_by_id = {r.id: r for r in roles}

        subjects_by_uuid = self._resolve_subjects(requests)

        for resource_type, resource_id in {(r.resource_type, r.resource_id) for r in requests}:
            self._validate_resource(resource_type, resource_id)

        for req in requests:
            self._validate_role_scopes([roles_by_uuid[req.role_id]], req.resource_type, req.resource_id)

        access_groups = self._group_by_subject_resource(requests, roles_by_uuid)
        all_tuples_to_add: list[RelationTuple] = []

        for key, role_ids in access_groups.items():
            subject_type, subject_id, resource_type, resource_id = key
            subject = subjects_by_uuid[subject_id]

            created, linked = self._add_access(subject, resource_type, resource_id, roles_by_id, role_ids)
            tuples_add, _ = RoleBinding.replication_tuples(
                subject=subject, bindings_created=created, subject_linked_to=linked
            )
            all_tuples_to_add.extend(tuples_add)

        self._replicate_tuples(all_tuples_to_add, [], ReplicationEventType.BATCH_CREATE_ROLE_BINDING)

        resource_names = self._compute_resource_names(requests)
        result = [
            {
                "role": roles_by_uuid[req.role_id],
                "subject_type": req.subject_type,
                "subject": subjects_by_uuid[req.subject_id],
                "resource_type": req.resource_type,
                "resource_id": req.resource_id,
                "resource_name": resource_names[(req.resource_type, req.resource_id)],
            }
            for req in requests
        ]

        logger.info("Created %d role binding(s) for tenant %s", len(result), self.tenant.org_id)
        return result

    def _resolve_subjects(self, requests: list[CreateBindingRequest]) -> dict[str, Group | Principal]:
        """Batch-resolve all subjects, raising NotFoundError for any missing."""
        group_uuids = {r.subject_id for r in requests if r.subject_type == SubjectType.GROUP}
        user_uuids = {r.subject_id for r in requests if r.subject_type == SubjectType.USER}

        groups_by_uuid = Subject.objects.groups(group_uuids)
        missing = group_uuids - set(groups_by_uuid.keys())
        if missing:
            raise NotFoundError(SubjectType.GROUP, ", ".join(missing))

        principals_by_uuid = Subject.objects.users(user_uuids)
        missing = user_uuids - set(principals_by_uuid.keys())
        if missing:
            raise NotFoundError(SubjectType.USER, ", ".join(missing))

        return {**groups_by_uuid, **principals_by_uuid}

    def _validate_subject(self, subject: Group | Principal):
        if not (isinstance(subject, Group) or isinstance(subject, Principal)):
            raise TypeError(f"Unexpected subject: {subject!r}")

        if not self._allow_external_subjects:
            if subject.tenant_id != self.tenant.id:
                subject_type = SubjectType.GROUP if isinstance(subject, Group) else SubjectType.USER
                raise NotFoundError(subject_type, str(subject.uuid))

    @staticmethod
    def _group_by_subject_resource(
        requests: list[CreateBindingRequest],
        roles_by_uuid: dict[str, RoleV2],
    ) -> dict[tuple, set[int]]:
        """Group requests into (subject_type, subject_id, resource_type, resource_id) -> role PKs."""
        groups: dict[tuple, set[int]] = {}
        for req in requests:
            key = (req.subject_type, req.subject_id, req.resource_type, req.resource_id)
            groups.setdefault(key, set()).add(roles_by_uuid[req.role_id].id)
        return groups

    def _compute_resource_names(self, requests: list[CreateBindingRequest]) -> dict[tuple[str, str], str | None]:
        """Resolve display names for each unique resource."""
        result: dict[tuple[str, str], str | None] = {}
        for req in requests:
            key = (req.resource_type, req.resource_id)
            if key not in result:
                result[key] = self.get_resource_name(req.resource_id, req.resource_type)
        return result

    def _build_base_queryset(
        self,
        resource_id: str,
        resource_type: str,
        binding_uuids: Optional[Sequence[str]] = None,
        exclude_direct: bool = False,
    ) -> QuerySet:
        """Build base queryset of groups with role bindings for a resource.

        Args:
            resource_id: The resource identifier
            resource_type: The type of resource
            binding_uuids: Optional list of binding UUIDs to include (for inherited bindings)
            exclude_direct: If True, exclude direct bindings and only show inherited

        Returns:
            Annotated QuerySet of Group objects
        """
        if exclude_direct and binding_uuids is None:
            # Relations API failed — cannot determine inherited bindings, return empty
            return Group.objects.none()
        elif exclude_direct and binding_uuids is not None:
            # Only inherited bindings by UUID (exclude direct)
            binding_filter = Q(role_binding_entries__binding__uuid__in=binding_uuids) & ~Q(
                role_binding_entries__binding__resource_type=resource_type,
                role_binding_entries__binding__resource_id=resource_id,
            )
        elif binding_uuids is not None:
            # Both direct and inherited bindings (exclude_sources=none)
            binding_filter = Q(
                role_binding_entries__binding__resource_type=resource_type,
                role_binding_entries__binding__resource_id=resource_id,
            ) | Q(role_binding_entries__binding__uuid__in=binding_uuids)
        else:
            # Only direct bindings (exclude_sources=indirect)
            binding_filter = Q(
                role_binding_entries__binding__resource_type=resource_type,
                role_binding_entries__binding__resource_id=resource_id,
            )

        # Get groups that have bindings matching our filter.
        # This includes tenant-specific groups and public tenant default groups.
        queryset = Group.objects.filter(binding_filter).distinct()

        if not self._allow_external_subjects:
            queryset = queryset.filter(
                Q(tenant=self.tenant) | Q(platform_default=True, system=True) | Q(admin_default=True, system=True)
            )

        # Annotate with principal count
        queryset = queryset.annotate(
            principalCount=Count("principals", filter=Q(principals__type=Principal.Types.USER), distinct=True)
        )

        # Prefetch role bindings for this resource with their roles
        # Also prefetch children for platform roles (which will be returned instead of the platform role)
        # When binding_uuids is provided (inherited bindings), include those bindings in the prefetch
        # so groups with only inherited roles get their roles populated correctly
        # Must respect exclude_direct to avoid including direct binding roles in the response
        binding_filter_q = self._build_binding_filter(
            resource_id=resource_id,
            resource_type=resource_type,
            binding_uuids=binding_uuids,
            exclude_direct=exclude_direct,
        )
        binding_queryset = (
            RoleBinding.objects.filter(binding_filter_q)
            .select_related("role")
            .prefetch_related("role__children")
            .with_resource_names()
        )

        # Prefetch the join table entries with the filtered bindings
        # Include both direct bindings and inherited bindings (when binding_uuids provided)
        # Must respect exclude_direct to avoid including direct binding roles in the response
        if exclude_direct:
            # Only inherited bindings - exclude any that match this resource directly
            rolebinding_group_filter = Q(binding__uuid__in=binding_uuids) & ~Q(
                binding__resource_type=resource_type, binding__resource_id=resource_id
            )
        else:
            rolebinding_group_filter = Q(binding__resource_type=resource_type, binding__resource_id=resource_id)
            if binding_uuids:
                rolebinding_group_filter = rolebinding_group_filter | Q(binding__uuid__in=binding_uuids)
        rolebinding_group_queryset = RoleBindingGroup.objects.filter(rolebinding_group_filter).prefetch_related(
            Prefetch("binding", queryset=binding_queryset)
        )

        queryset = queryset.prefetch_related(
            Prefetch(
                "role_binding_entries",
                queryset=rolebinding_group_queryset,
                to_attr="filtered_bindings",
            )
        )

        # Annotate with latest modified timestamp from roles
        # Include inherited bindings when binding_uuids is provided
        if binding_uuids:
            latest_modified_filter = Q(
                role_binding_entries__binding__resource_type=resource_type,
                role_binding_entries__binding__resource_id=resource_id,
            ) | Q(role_binding_entries__binding__uuid__in=binding_uuids)
        else:
            latest_modified_filter = Q(
                role_binding_entries__binding__resource_type=resource_type,
                role_binding_entries__binding__resource_id=resource_id,
            )

        queryset = queryset.annotate(
            latest_modified=Max("role_binding_entries__created", filter=latest_modified_filter)
        )

        queryset = self._annotate_role_fields_for_cursor(queryset, latest_modified_filter)

        return queryset

    def _annotate_role_fields_for_cursor(self, queryset: QuerySet, latest_modified_filter: Q) -> QuerySet:
        """Annotate role fields so CursorPagination can extract cursor positions.

        DRF's CursorPagination uses getattr(instance, field) to build cursor
        positions. ORM lookups like role_binding_entries__binding__role__name
        work in .order_by() but fail in getattr(). Min is used because a
        subject may have multiple bindings (multi-valued). UUID needs Cast
        to text because PostgreSQL has no MIN(uuid).
        """
        role_field_base = "role_binding_entries__binding__role__{}"
        return queryset.annotate(
            **{
                f"role_binding_entries__binding__role__{field}": Min(
                    role_field_base.format(field), filter=latest_modified_filter
                )
                for field in ("name", "modified", "created")
            },
            **{
                "role_binding_entries__binding__role__uuid": Min(
                    Cast(role_field_base.format("uuid"), CharField()), filter=latest_modified_filter
                )
            },
        )

    def _apply_subject_filters(
        self,
        queryset: QuerySet,
        subject_type: Optional[str],
        subject_id: Optional[str],
    ) -> QuerySet:
        """Apply subject type and ID filters to group queryset.

        Args:
            queryset: Base queryset to filter (Group objects)
            subject_type: Optional subject type filter (e.g., 'group')
            subject_id: Optional subject ID filter

        Returns:
            Filtered queryset
        """
        if subject_type:
            # For group queryset, only 'group' subject type is valid
            # 'user' type is handled separately in _build_user_queryset
            if subject_type != SubjectType.GROUP:
                # Filter out all results for unsupported subject types
                return queryset.none()

        if subject_id:
            queryset = queryset.filter(uuid=subject_id)

        return queryset

    def _build_binding_filter(
        self,
        resource_id: str,
        resource_type: str,
        binding_uuids: Optional[Sequence[str]] = None,
        exclude_direct: bool = False,
    ) -> Q:
        direct_filter = Q(resource_type=resource_type, resource_id=resource_id)

        if binding_uuids is not None:
            indirect_filter = Q(uuid__in=binding_uuids)

            if exclude_direct:
                return indirect_filter & ~direct_filter
            else:
                return indirect_filter | direct_filter
        else:
            if exclude_direct:
                raise ValueError("Cannot have exclude_direct be True when no binding_uuids are provided.")

            return direct_filter

    def _build_user_queryset(
        self,
        resource_id: str,
        resource_type: str,
        binding_uuids: Optional[Sequence[str]] = None,
        exclude_direct: bool = False,
    ) -> QuerySet:
        """Build queryset of users (principals) with role bindings for a resource.

        Users are queried directly via RoleBindingPrincipal, not through group memberships.

        Args:
            resource_id: The resource identifier
            resource_type: The type of resource
            binding_uuids: Optional list of binding UUIDs to include (for inherited bindings)
            exclude_direct: If True, exclude direct bindings and only show inherited

        Returns:
            Annotated QuerySet of Principal objects (users only)
        """
        # Build filter for bindings based on exclude_direct and binding_uuids
        if exclude_direct and binding_uuids is None:
            # Relations API failed — cannot determine inherited bindings, return empty
            return Principal.objects.none()

        binding_filter = self._build_binding_filter(
            resource_id=resource_id,
            resource_type=resource_type,
            binding_uuids=binding_uuids,
            exclude_direct=exclude_direct,
        )

        principal_entry_filter = Q(
            source=self._principal_source,
            binding__in=RoleBinding.objects.filter(binding_filter),
        )

        # Get users who have role bindings matching our filter
        principal_queryset = Principal.objects.filter(
            Exists(RoleBindingPrincipal.objects.filter(principal=OuterRef("pk")).filter(principal_entry_filter)),
        ).distinct()

        # Always filter to USER type (this endpoint is for user subjects only)
        principal_queryset = principal_queryset.filter(type=Principal.Types.USER)

        if not self._allow_external_subjects:
            principal_queryset = principal_queryset.filter(tenant=self.tenant)

        # Prefetch RoleBindingPrincipal entries with their bindings
        principal_entry_queryset = RoleBindingPrincipal.objects.filter(principal_entry_filter).prefetch_related(
            Prefetch(
                "binding",
                queryset=RoleBinding.objects.select_related("role")
                .prefetch_related("role__children")
                .with_resource_names(),
            )
        )

        # Prefetch role_binding_entries on Principal
        principal_queryset = principal_queryset.prefetch_related(
            Prefetch(
                "role_binding_entries",
                queryset=principal_entry_queryset,
                to_attr="filtered_bindings",
            )
        )

        # Annotate with latest modified timestamp from roles
        latest_modified_filter = Q(
            role_binding_entries__in=RoleBindingPrincipal.objects.filter(principal_entry_filter)
        )

        principal_queryset = principal_queryset.annotate(
            latest_modified=Max(
                "role_binding_entries__created",
                filter=latest_modified_filter,
            )
        )

        principal_queryset = self._annotate_role_fields_for_cursor(principal_queryset, latest_modified_filter)

        return principal_queryset

    def _apply_user_filters(
        self,
        queryset: QuerySet,
        subject_id: Optional[str],
    ) -> QuerySet:
        """Apply filters to user queryset.

        Args:
            queryset: Base queryset to filter (Principal objects)
            subject_id: Optional subject ID filter (UUID)

        Returns:
            Filtered queryset
        """
        if subject_id:
            queryset = queryset.filter(uuid=subject_id)

        return queryset

    def _lookup_binding_uuids_via_relations(self, resource_type: str, resource_id: str) -> Optional[list[str]]:
        """Use the Relations API to resolve binding UUIDs that affect the given resource.

        Uses the recursive 'binding' relation to find role_bindings on this resource
        and any parent resources in the hierarchy.
        """
        return lookup_binding_subjects(resource_type, resource_id)

    def _ensure_default_bindings_exist(self) -> None:
        """Lazily create default role bindings if they don't exist.

        This method checks if the tenant has default role bindings by counting ADMIN bindings.
        ADMIN bindings are always created (regardless of custom groups), so if all 3 ADMIN
        bindings exist, we know default bindings have already been processed for this tenant.

        If bindings don't exist, it creates all missing bindings in a single atomic transaction.
        USER bindings are only created if the tenant has no custom default group.
        """
        try:
            mapping = self.tenant.tenant_mapping
        except TenantMapping.DoesNotExist:
            logger.debug(f"No tenant mapping for tenant {self.tenant.org_id}, skipping default bindings")
            return

        # Fast path: check if ADMIN bindings exist (always created regardless of custom group)
        # If all ADMIN bindings exist, default bindings have been processed for this tenant
        admin_binding_uuids = [mapping.default_role_binding_uuid_for(DefaultAccessType.ADMIN, s) for s in Scope]
        existing_admin_count = RoleBinding.objects.filter(uuid__in=admin_binding_uuids).count()

        if existing_admin_count == len(Scope):
            # All ADMIN bindings exist - default bindings already processed
            return

        # ADMIN bindings don't all exist - need to create missing bindings
        # Now check for custom default group (only needed when creating)
        has_custom_default_group = self._has_custom_default_group()

        # Create all missing bindings in a single transaction
        try:
            self._create_all_default_bindings(mapping, skip_user_bindings=has_custom_default_group)
        except DefaultGroupNotAvailableError:
            logger.warning(
                f"Platform roles not available, skipping default binding creation for tenant {self.tenant.org_id}"
            )
        except Exception as e:
            error_msg = f"Failed to create default bindings for tenant {self.tenant.org_id}: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

    def _has_custom_default_group(self) -> bool:
        """Check if the tenant has a custom default group.

        A custom default group is a Group with platform_default=True that belongs
        to this tenant (not the public tenant).

        Returns:
            True if tenant has a custom default group, False otherwise
        """
        return Group.objects.filter(tenant=self.tenant, platform_default=True).exists()

    @transaction.atomic
    def _create_all_default_bindings(self, mapping: TenantMapping, skip_user_bindings: bool = False) -> None:
        """Create all default role bindings for all scopes in a single transaction.

        Creates admin default bindings for all 3 scopes, and optionally user default bindings
        (unless the tenant has a custom default group).

        Args:
            mapping: The tenant mapping containing default UUIDs
            skip_user_bindings: If True, skip creating USER default bindings
                (used when tenant has custom default group)
        """
        policy_service = GlobalPolicyIdService.shared()
        created_count = 0

        for scope in Scope:
            # Get resource info for this scope
            resource_type, resource_id = self._get_resource_for_scope(scope)
            if resource_id is None:
                logger.warning(f"Could not determine resource for scope {scope} in tenant {self.tenant.org_id}")
                continue

            for access_type in DefaultAccessType:
                # Skip USER bindings if tenant has custom default group
                if skip_user_bindings and access_type == DefaultAccessType.USER:
                    continue

                # Check if this specific binding already exists
                binding_uuid = mapping.default_role_binding_uuid_for(access_type, scope)
                if RoleBinding.objects.filter(uuid=binding_uuid).exists():
                    continue

                self._create_single_default_binding(
                    mapping=mapping,
                    access_type=access_type,
                    scope=scope,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    policy_service=policy_service,
                )
                created_count += 1

        if created_count > 0:
            logger.info(f"Created {created_count} default role bindings for tenant {self.tenant.org_id}")

    def _create_single_default_binding(
        self,
        mapping: TenantMapping,
        access_type: DefaultAccessType,
        scope: Scope,
        resource_type: str,
        resource_id: str,
        policy_service: GlobalPolicyIdService,
    ) -> None:
        """Create a single default role binding with its associated group.

        Uses the platform default groups from the public tenant rather than
        creating per-tenant groups.

        Args:
            mapping: The tenant mapping
            access_type: USER or ADMIN
            scope: The scope level
            resource_type: The resource type string
            resource_id: The resource ID string
            policy_service: Service for looking up platform role UUIDs
        """
        binding_uuid = mapping.default_role_binding_uuid_for(access_type, scope)

        # Get the platform role
        platform_role_uuid = platform_v2_role_uuid_for(access_type, scope, policy_service)
        try:
            platform_role = PlatformRoleV2.objects.get(uuid=platform_role_uuid)
        except PlatformRoleV2.DoesNotExist:
            logger.error(f"Platform role {platform_role_uuid} not found for {access_type} {scope}")
            raise DefaultGroupNotAvailableError(f"Platform role not found: {platform_role_uuid}")

        # Get the platform default group from public tenant (created by seed_group)
        if access_type == DefaultAccessType.ADMIN:
            group = Group.admin_default_set().public_tenant_only().get()
        else:
            group = Group.platform_default_set().public_tenant_only().get()

        # Create the role binding
        binding, created = RoleBinding.objects.get_or_create(
            uuid=binding_uuid,
            defaults={
                "role": platform_role,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "tenant": self.tenant,
            },
        )

        if created:
            # Create the binding-group relationship
            RoleBindingGroup.objects.get_or_create(
                group=group,
                binding=binding,
            )

    def _get_resource_for_scope(self, scope: Scope) -> tuple[str, Optional[str]]:
        """Get the resource type and ID for a given scope.

        Args:
            scope: The scope level

        Returns:
            Tuple of (resource_type, resource_id). resource_id may be None if not found.
        """
        if scope == Scope.TENANT:
            return ("tenant", self.tenant.tenant_resource_id())

        if scope == Scope.ROOT:
            try:
                workspace = Workspace.objects.root(tenant=self.tenant)
                return ("workspace", str(workspace.id))
            except Workspace.DoesNotExist:
                return ("workspace", None)

        if scope == Scope.DEFAULT:
            try:
                workspace = Workspace.objects.default(tenant=self.tenant)
                return ("workspace", str(workspace.id))
            except Workspace.DoesNotExist:
                return ("workspace", None)

        return ("unknown", None)

    @transaction.atomic
    def delete_user_default_bindings(self) -> None:
        """Delete USER default role bindings for the tenant.

        This should be called when a custom default group is created for the tenant.
        ADMIN default bindings are NOT affected (admin access is not customizable).

        Note: This only deletes the RoleBinding and RoleBindingGroup records.
        The public tenant's platform_default Group is shared and not deleted.
        """
        try:
            mapping = self.tenant.tenant_mapping
        except TenantMapping.DoesNotExist:
            logger.debug(f"No tenant mapping for tenant {self.tenant.org_id}, nothing to delete")
            return

        # Get all USER binding UUIDs
        user_binding_uuids = [mapping.default_role_binding_uuid_for(DefaultAccessType.USER, scope) for scope in Scope]

        # Delete all in one query (RoleBindingGroup entries cascade)
        deleted_count, _ = RoleBinding.objects.filter(uuid__in=user_binding_uuids).delete()

        if deleted_count > 0:
            logger.info(
                f"Deleted {deleted_count} USER default role bindings for tenant {self.tenant.org_id} "
                f"(custom default group created)"
            )

    @transaction.atomic
    def restore_user_default_bindings(self) -> None:
        """Restore USER default role bindings for the tenant.

        This should be called when a custom default group is deleted for the tenant.
        Creates USER default bindings for all scopes (DEFAULT, ROOT, TENANT).

        Note: ADMIN default bindings are not affected by this method.
        """
        try:
            mapping = self.tenant.tenant_mapping
        except TenantMapping.DoesNotExist:
            logger.debug(f"No tenant mapping for tenant {self.tenant.org_id}, cannot restore bindings")
            return

        try:
            policy_service = GlobalPolicyIdService.shared()

            for scope in Scope:
                # Check if binding already exists
                binding_uuid = mapping.default_role_binding_uuid_for(DefaultAccessType.USER, scope)
                if RoleBinding.objects.filter(uuid=binding_uuid).exists():
                    continue

                resource_type, resource_id = self._get_resource_for_scope(scope)
                if resource_id is None:
                    logger.warning(f"Could not determine resource for scope {scope} in tenant {self.tenant.org_id}")
                    continue

                self._create_single_default_binding(
                    mapping=mapping,
                    access_type=DefaultAccessType.USER,
                    scope=scope,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    policy_service=policy_service,
                )

            logger.info(
                f"Restored USER default role bindings for tenant {self.tenant.org_id} "
                f"(custom default group deleted)"
            )
        except DefaultGroupNotAvailableError:
            logger.warning(
                f"Platform roles not available, could not restore default bindings for tenant {self.tenant.org_id}"
            )
        except Exception as e:
            logger.error(f"Failed to restore default bindings for tenant {self.tenant.org_id}: {e}")

    def _maybe_customize_default_group(self, subject: Subject) -> tuple[Subject, Optional[Group]]:
        """If subject is the public tenant's default access group, customize it for this tenant.

        When modifying role bindings for the public tenant's system default access group,
        we create a tenant-specific custom default access group (mirroring the V1 behavior
        in clone_default_group_in_public_schema).

        If the tenant already has a custom default group, returns that instead.

        Returns the original subject unchanged when it is not the public default group.

        Returns:
            A tuple of (subject, created_group). created_group is the newly created custom
            default group if one was created, or None otherwise.
        """
        if not subject.is_group:
            return subject, None
        group = subject.entity
        if group.admin_default:
            raise InvalidFieldError(
                "subject_id",
                "Role bindings for the admin default group cannot be modified.",
            )
        if not (group.platform_default and group.system):
            return subject, None

        existing_custom = Group.objects.filter(platform_default=True, tenant=self.tenant).first()
        if existing_custom is not None:
            return Subject(type=SubjectType.GROUP, entity=existing_custom), None

        from management.group.definer import clone_default_group_in_public_schema

        custom_group = clone_default_group_in_public_schema(group, self.tenant)
        if custom_group is None:
            raise RuntimeError(f"Failed to create custom default access group for tenant {self.tenant.org_id}")

        return Subject(type=SubjectType.GROUP, entity=custom_group), custom_group

    @atomic
    def update_role_bindings_for_subject(
        self,
        resource_type: str,
        resource_id: str,
        subject_type: str,
        subject_id: str,
        role_ids: list[str],
    ) -> UpdateRoleBindingResult:
        """Update role bindings for a subject on a resource.

        This replaces all existing role bindings for the subject on the resource
        with the provided roles.

        If the subject is the public tenant's default access group, it is first
        cloned into a tenant-specific custom default access group (matching the
        V1 behavior), and the role binding update is applied to that custom group.

        Args:
            resource_type: The type of resource (e.g., 'workspace')
            resource_id: The resource identifier
            subject_type: The type of subject ('group' or 'user')
            subject_id: The subject identifier (UUID)
            role_ids: List of role UUIDs to assign

        Returns:
            UpdateRoleBindingResult with the updated binding information

        Raises:
            UnsupportedSubjectTypeError: If the subject type is not supported
            NotFoundError: If the subject or resource cannot be found
            InvalidFieldError: If one or more roles cannot be found
        """
        self._validate_resource(resource_type, resource_id)
        ensure_v2_write_activated(self.tenant)

        roles = self._get_roles(role_ids)
        self._validate_role_scopes(roles, resource_type, resource_id)

        subject = Subject.objects.by_type(type=subject_type, id=subject_id)

        subject, created_custom_group = self._maybe_customize_default_group(subject)

        self._validate_subject(subject.entity)

        self._replace_role_bindings(
            resource_type=resource_type,
            resource_id=resource_id,
            subject=subject.entity,
            roles=roles,
        )

        result = UpdateRoleBindingResult(
            subject_type=subject_type,
            roles=roles,
            resource_id=resource_id,
            resource_type=resource_type,
            subject=subject.entity,
            resource_name=self.get_resource_name(resource_id, resource_type),
            custom_default_group_created=created_custom_group,
        )

        logger.info(
            "Updated role bindings for %s '%s' on %s '%s': %d roles assigned",
            subject_type,
            subject_id,
            resource_type,
            resource_id,
            len(roles),
        )

        return result

    def _validate_resource(self, resource_type: str, resource_id: str) -> None:
        """Validate that the resource exists and belongs to this tenant.

        Only resource types with a local table (currently workspace) are
        checked for existence.  All other types are accepted as is because
        there is no local record to validate against.

        TODO: check in inventory API for existence of the resource.

        Args:
            resource_type: The type of resource (e.g., ``"workspace"``)
            resource_id: The resource identifier

        Raises:
            RequiredFieldError: If resource_type or resource_id is empty
            NotFoundError: If the resource cannot be found for this tenant
        """
        if not resource_type:
            raise RequiredFieldError("resource_type")

        if not resource_id:
            raise RequiredFieldError("resource_id")

        if resource_type == "workspace":
            if not Workspace.objects.exists_for_tenant(resource_id, tenant=self.tenant):
                raise NotFoundError(resource_type, resource_id)

        if resource_type == "tenant":
            expected_resource_id = self.tenant.tenant_resource_id()
            if expected_resource_id is None or resource_id != expected_resource_id:
                raise NotFoundError(resource_type, resource_id)

    def _get_roles(self, role_ids: list[str]) -> list[RoleV2]:
        """Get assignable roles by their UUIDs, validating all exist.

        Uses RoleV2.objects.assignable() to filter to roles that can be
        assigned to bindings (custom + seeded, not platform).
        Also excludes roles with permissions from migration-excluded applications.
        Empty role_ids is valid and returns [] to support removing all bindings.

        Raises:
            InvalidFieldError: If any requested role UUIDs don't exist or aren't assignable
        """
        if not role_ids:
            return []

        roles = list(RoleV2.objects.filter(uuid__in=role_ids).assignable().excluding_out_of_scope_v2_roles())

        found_ids = {str(r.uuid) for r in roles}
        requested_ids = set(role_ids)

        if found_ids != requested_ids:
            missing = list(requested_ids - found_ids)
            raise InvalidFieldError("roles", f"The following roles do not exist: {', '.join(missing)}")

        return roles

    def _validate_role_scopes(self, roles: list[RoleV2], resource_type: str, resource_id: str) -> None:
        """Validate that each role's highest scope matches the target resource.

        Uses ``ImplicitResourceService.highest_scope_for_permissions`` to compute
        the scope from each role's permission strings.

        For standard (child) workspaces, additionally rejects roles that have
        any explicit-DEFAULT permissions or no permissions at all, since those
        represent grants no service checks at that workspace level.

        Must be called after ``_validate_resource`` which ensures the resource
        exists. Empty *roles* is valid (means "unbind all") and skips the check.

        Raises:
            InvalidFieldError: If any role's scope does not match the resource.
            NotFoundError: If the resource cannot be resolved to a scope (should
                not happen when ``_validate_resource`` ran first).
        """
        if self._skip_scope_validation or not roles:
            return

        is_standard_workspace = False
        expected: Scope
        if resource_type == "workspace":
            result = resolve_workspace_scope(resource_id, self.tenant)
            if result is None:
                raise NotFoundError(resource_type, resource_id)
            expected, is_standard_workspace = result
        else:
            resolved = scope_for_resource(resource_type, resource_id, self.tenant)
            if resolved is None:
                if resource_type == "tenant":
                    raise NotFoundError(resource_type, resource_id)
                return
            expected = resolved

        explicit_default_ids = permission_scope_cache.explicit_default_ids if is_standard_workspace else frozenset()

        mismatched: list[str] = []
        for role in roles:
            perm_rows = list(role.permissions.values_list("id", "permission"))
            perm_strings = [row[1] for row in perm_rows]
            role_scope = default_implicit_resource_service.highest_scope_for_permissions(perm_strings)
            if role_scope != expected:
                mismatched.append(f"{role.name} ({role.uuid})")
            elif is_standard_workspace:
                if not perm_rows:
                    mismatched.append(f"{role.name} ({role.uuid})")
                elif explicit_default_ids:
                    perm_ids = {row[0] for row in perm_rows}
                    if perm_ids & explicit_default_ids:
                        mismatched.append(f"{role.name} ({role.uuid})")

        if mismatched:
            scope_label = "Standard Workspace" if is_standard_workspace else SCOPE_DISPLAY_NAME[expected]
            raise InvalidFieldError(
                "roles",
                f"The following roles are not scoped for this resource ({scope_label}): {', '.join(mismatched)}",
            )

    def _replace_role_bindings(
        self,
        resource_type: str,
        resource_id: str,
        subject: Group | Principal,
        roles: Sequence[RoleV2],
    ) -> None:
        self._validate_subject(subject)

        """Replace all role bindings for a subject on a resource.

        Computes the diff between current and desired roles, then only
        adds/removes what actually changed. No-ops when the state already matches.

        TODO: Refactor to move single-instance business logic (e.g. subject
        linking/unlinking) into the RoleBinding model, keeping only
        cross-instance CRUD orchestration in the service.

        Args:
            resource_type: The type of resource (e.g., 'workspace')
            resource_id: The resource identifier
            subject: The subject (Group or Principal) to update bindings for
            roles: The roles to assign to the subject
        """
        # 1. Query current bindings for this subject on this resource
        current_bindings = RoleBinding.objects.for_resource(resource_type, resource_id, self.tenant)
        if isinstance(subject, Group):
            current_bindings = current_bindings.filter(group_entries__group=subject)
        else:
            current_bindings = current_bindings.filter(
                principal_entries__principal=subject, principal_entries__source=self._principal_source
            )

        # 2. Compute the diff
        current_role_ids = {b.role_id for b in current_bindings}
        desired_role_ids = {r.id for r in roles}

        access_to_add = desired_role_ids - current_role_ids
        access_to_remove = current_role_ids - desired_role_ids

        # 3. No-op: nothing to add or remove
        if not access_to_add and not access_to_remove:
            return

        bindings_created: list[RoleBinding] = []
        bindings_deleted: list[RoleBinding] = []
        subject_linked_to: list[RoleBinding] = []
        subject_unlinked_from: list[RoleBinding] = []

        # 4. Remove: unlink subject from roles no longer desired,
        #    then delete any bindings left with no subjects attached.
        if access_to_remove:
            bindings_to_remove = [b for b in current_bindings if b.role_id in access_to_remove]
            orphaned, unlinked = self._remove_access(subject, bindings_to_remove)
            bindings_deleted.extend(orphaned)
            subject_unlinked_from.extend(unlinked)

        # 5. Add: create bindings for newly desired roles and link subject
        if access_to_add:
            roles_by_id = {r.id: r for r in roles}
            created, linked = self._add_access(subject, resource_type, resource_id, roles_by_id, access_to_add)
            bindings_created.extend(created)
            subject_linked_to.extend(linked)

        # 6. Compute replication tuples from the changeset (pure model logic)
        tuples_to_add, tuples_to_remove = RoleBinding.replication_tuples(
            subject=subject,
            bindings_created=bindings_created,
            bindings_deleted=bindings_deleted,
            subject_linked_to=subject_linked_to,
            subject_unlinked_from=subject_unlinked_from,
        )

        # 7. Replicate to SpiceDB via the outbox
        self._replicate_tuples(tuples_to_add, tuples_to_remove, ReplicationEventType.UPDATE_ROLE_BINDINGS_FOR_SUBJECT)

    def _remove_access(
        self,
        subject: Group | Principal,
        bindings: Sequence[RoleBinding],
    ) -> tuple[list[RoleBinding], list[RoleBinding]]:
        self._validate_subject(subject)

        """Remove a subject from bindings, cleaning up orphaned bindings.

        Returns:
            (orphaned_bindings, unlinked_bindings):
            - orphaned_bindings: bindings that had no subjects left and were deleted
            - unlinked_bindings: bindings the subject was unlinked from
        """
        binding_ids = [b.id for b in bindings]

        # 1. Unlink: remove subject from these bindings
        if isinstance(subject, Group):
            RoleBindingGroup.objects.filter(group=subject, binding_id__in=binding_ids).delete()
        else:
            RoleBindingPrincipal.objects.filter(
                principal=subject, binding_id__in=binding_ids, source=self._principal_source
            ).delete()

        # 2. Cleanup: delete any bindings that are now orphaned
        orphaned = list(RoleBinding.objects.filter(id__in=binding_ids).orphaned())

        if orphaned:
            RoleBinding.objects.filter(pk__in=[b.pk for b in orphaned]).delete()

        if isinstance(subject, Group):
            # We always remove all group entries, so all provided bindings will be unlinked.
            unlinked = list(bindings)
        else:
            # For principals, it's possible that another entry exists with a source that we cannot remove,
            # so the principal may not have become unlinked from all the provided bindings.
            #
            # A binding that is fully removed has still been "unlinked" from its subject, so we need to include it
            # here. (Otherwise, we will not actually remove the relevant subject relation.)
            unlinked = orphaned + list(
                RoleBinding.objects.filter(id__in=binding_ids).exclude(principal_entries__principal=subject)
            )

        return orphaned, unlinked

    def _add_access(
        self,
        subject: Group | Principal,
        resource_type: str,
        resource_id: str,
        roles_by_id: dict,
        role_ids: set,
    ) -> tuple[list[RoleBinding], list[RoleBinding]]:
        self._validate_subject(subject)

        """Create or find bindings for roles and link the subject.

        Uses bulk operations to minimise DB round-trips:
        1 query  — find existing bindings for these roles on this resource
        1 insert — bulk-create any missing bindings
        1 insert — bulk-create through-model links (ignore_conflicts for safety)

        Returns:
            (created_bindings, linked_bindings):
            - created_bindings: newly created RoleBinding instances
            - linked_bindings: all bindings the subject was linked to
        """
        # 1. Find existing bindings for these roles on this resource
        existing = list(
            RoleBinding.objects.for_resource(resource_type, resource_id, self.tenant).filter(role_id__in=role_ids)
        )
        existing_role_ids = {b.role_id for b in existing}

        # 2. Bulk-create bindings for roles that don't have one yet
        new_bindings: list[RoleBinding] = []
        new_role_ids = role_ids - existing_role_ids
        if new_role_ids:
            new_bindings = RoleBinding.objects.bulk_create(
                [
                    RoleBinding(
                        role=roles_by_id[rid],
                        resource_type=resource_type,
                        resource_id=resource_id,
                        tenant=self.tenant,
                    )
                    for rid in new_role_ids
                ],
            )
            existing.extend(new_bindings)

        # 3. Link subject to all bindings in one bulk insert
        if isinstance(subject, Group):
            RoleBindingGroup.objects.bulk_create(
                [RoleBindingGroup(binding=b, group=subject) for b in existing],
                ignore_conflicts=True,
            )
        else:
            RoleBindingPrincipal.objects.bulk_create(
                [RoleBindingPrincipal(binding=b, principal=subject, source=self._principal_source) for b in existing],
                ignore_conflicts=True,
            )

        return new_bindings, existing

    def _replicate_tuples(
        self,
        tuples_to_add: list[RelationTuple],
        tuples_to_remove: list[RelationTuple],
        event_type: ReplicationEventType,
    ) -> None:
        """Replicate relation tuple changes to SpiceDB via the outbox.

        Writes a ``ReplicationEvent`` to the outbox table within the
        current transaction.  The async Debezium worker picks it up
        and sends it to SpiceDB.

        No-ops when both lists are empty (avoids an empty outbox row).

        Args:
            tuples_to_add: Relation tuples to add.
            tuples_to_remove: Relation tuples to remove.
            event_type: The replication event type.
        """
        if not tuples_to_add and not tuples_to_remove:
            return

        self._replicator.replicate(
            ReplicationEvent(
                event_type=event_type,
                info={"org_id": str(self.tenant.org_id)},
                partition_key=PartitionKey.byEnvironment(),
                add=tuples_to_add,
                remove=tuples_to_remove,
            )
        )
