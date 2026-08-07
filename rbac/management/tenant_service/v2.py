"""V2 implementation of Tenant bootstrapping."""

import dataclasses
from typing import Callable, Iterable, List, Optional

from django.conf import settings
from django.db.models import Prefetch, Q, QuerySet
from management.group.model import Group
from management.group.platform import DefaultGroupNotAvailableError, GlobalPolicyIdService
from management.permission.scope_service import TenantScopeResources
from management.principal.model import Principal
from management.relation_replicator.relation_replicator import (
    PartitionKey,
    RelationReplicator,
    ReplicationEvent,
    ReplicationEventType,
    WorkspaceEventStream,
)
from management.relation_replicator.types import RelationTuple
from management.tenant_mapping.model import DefaultAccessType, TenantMapping, logger
from management.tenant_service.relations import default_role_binding_tuples
from management.tenant_service.tenant_service import BootstrappedTenant
from management.tenant_service.tenant_service import _ensure_principal_with_user_id_in_tenant
from management.workspace.model import Workspace
from management.workspace.utils.event import make_workspace_event
from migration_tool.utils import create_relationship


from api.models import Tenant, User


def default_get_user_id(user: User):
    """Get user ID."""
    if user.user_id is None:
        raise ValueError(f"Cannot update user without user_id. username={user.username}")
    return user.user_id


@dataclasses.dataclass(frozen=True)
class TenantBootstrapLock:
    """
    Contains a tenant's locked TenantMapping and custom default group (if any).

    This is returned from functions that take a tenant's bootstrap lock.
    """

    tenant_mapping: TenantMapping
    custom_default_group: Optional[Group]


def try_lock_tenants_for_bootstrap(tenants: Iterable[Tenant]) -> dict[Tenant, Optional[TenantBootstrapLock]]:
    """
    Lock the provided tenants in order to prevent concurrent V2 bootstrapping.

    In particular, this locks the tenant's TenantMapping and custom default group (if any). The returned dict
    is keyed by each tenant and contains a TenantBootstrapLock for each bootstrapped tenant. (It contains None for each
    tenant without a TenantMapping).

    This lock prevents the following from happening concurrently:
    * V2 bootstrapping of the tenant.
    * Creation of a custom default group for the tenant (since clone_default_group_in_public_schema holds this lock).
    * Deletion of a custom default group for the tenant (since any custom default group is locked).
    """
    tenants = list(tenants)

    if any(t.pk is None for t in tenants):
        raise ValueError("Cannot lock unsaved tenant")

    if any(t.tenant_name == "public" for t in tenants):
        raise ValueError("Cannot lock public tenant")

    mappings: dict[int, TenantMapping] = {
        m.tenant_id: m for m in TenantMapping.objects.select_for_update().filter(tenant__in=tenants)
    }

    # This prevents concurrent deletion of a custom default group because GroupViewSet uses select_for_update in
    # get_queryset when removing a group.
    #
    # Note that if new code that removes a custom default group is added, it must also ensure that it locks the
    # group. Locking the group here (but not *before* deletion) is insufficient because the deletion code could
    # concurrently replicate the removal of the group and the restoration of default access.
    #
    # Let T be a tenant with custom default group G. Consider transactions A (deleting a custom default group), B (also
    # deleting the same group), and C (creating a new custom default group), assuming a custom default group already
    # exists:
    #
    # A: Creates outbox message for deleting G (and restoring platform default access for T).
    # B: Creates outbox message for deleting G (and restoring platform default access for T).
    # B: Deletes custom default group G.
    # B: Commits.
    # C: Locks T for bootstrap.
    # C: Creates a new custom default group G'.
    # C: Creates outbox message for creating G' (and removing platform default access for T).
    # A: Attempts to delete G (to no effect, since it's already deleted).
    # A: Commits.
    #
    # Transaction A commits its platform default access restoration last, so tenant T will end up both having a custom
    # default access group G' *and* having platform default access (or, at best, the restoration and removal of
    # platform default access are unordered). This would result in inconsistency. Locking G before replicating the
    # platform default access restoration would prevent this issue by serializing A and B.

    default_groups: dict[int, Group] = {
        g.tenant_id: g for g in Group.objects.select_for_update().filter(platform_default=True, tenant__in=tenants)
    }

    result: dict[Tenant, Optional[TenantBootstrapLock]] = {}

    for tenant in tenants:
        mapping = mappings.get(tenant.id)

        if mapping is None:
            result[tenant] = None
            continue

        result[tenant] = TenantBootstrapLock(
            tenant_mapping=mapping, custom_default_group=default_groups.get(tenant.id)
        )

    return result


class TenantNotBootstrappedError(Exception):
    """Raised when a tenant is required to have been bootstrapped but has not been."""

    pass


def try_lock_tenant_for_bootstrap(tenant: Tenant) -> Optional[TenantBootstrapLock]:
    """Attempt to lock a single tenant, as if by try_lock_tenants_for_bootstrap."""
    return try_lock_tenants_for_bootstrap([tenant])[tenant]


def lock_tenant_for_bootstrap(tenant: Tenant) -> TenantBootstrapLock:
    """
    Lock a single tenant, as if by try_lock_tenant_for_bootstrap.

    This throws TenantNotBootstrappedError if the tenant is not bootstrapped.
    """
    result = try_lock_tenant_for_bootstrap(tenant)

    if result is None:
        raise TenantNotBootstrappedError(f"Tenant {tenant} not bootstrapped.")

    return result


@dataclasses.dataclass(frozen=True)
class _BootstrapReplicationEntry:
    tenant: Tenant
    default_workspace: Workspace

    def __post_init__(self):
        if not isinstance(self.tenant, Tenant):
            raise TypeError(f"Expected tenant, but got: {self.tenant!r}")

        if not isinstance(self.default_workspace, Workspace):
            raise TypeError(f"Expected default_workspace to be a workspace, but got: {self.default_workspace!r}")

        if self.default_workspace.tenant_id != self.tenant.id:
            raise ValueError(
                f"Expected default_workspace to be in tenant with id={self.tenant.id}, "
                f"but got tenant with id={self.default_workspace.tenant_id}"
            )

        if self.default_workspace.type != Workspace.Types.DEFAULT:
            raise ValueError(
                f"Expected default_workspace to be a default workspace, but was {self.default_workspace.type}"
            )


class V2TenantBootstrapService:
    """Service for bootstrapping tenants with built-in relationships."""

    _replicator: RelationReplicator
    _public_tenant: Optional[Tenant]
    _policy_service: GlobalPolicyIdService

    def __init__(
        self,
        replicator: RelationReplicator,
        public_tenant: Optional[Tenant] = None,
        get_user_id: Optional[Callable[[User], str]] = None,
    ):
        """Initialize the TenantBootstrapService with a RelationReplicator."""
        self._replicator = replicator
        self._public_tenant = public_tenant
        self._get_user_id = get_user_id if get_user_id else default_get_user_id
        self._policy_service = GlobalPolicyIdService.shared()

    def new_bootstrapped_tenant(self, org_id: str, account_number: Optional[str] = None) -> BootstrappedTenant:
        """Create a new tenant."""
        tenant = Tenant.objects.create(org_id=org_id, account_id=account_number)
        return self._bootstrap_tenant(tenant)

    def bootstrap_tenant(self, tenant: Tenant, force: bool = False) -> BootstrappedTenant:
        """
        Bootstrap an existing tenant.

        If [force] is True, will re-bootstrap the tenant if already bootstrapped.
        This does not change the RBAC data that already exists, but will replicate to Relations.
        """
        lock_result = try_lock_tenant_for_bootstrap(tenant)

        if lock_result is None:
            return self._bootstrap_tenant(tenant)

        if force:
            self._replicate_bootstraps([(tenant, lock_result.tenant_mapping)], bulk=False)

        return BootstrappedTenant(tenant=tenant, mapping=lock_result.tenant_mapping)

    def bootstrap_tenants(self, tenants: Iterable[Tenant], force: bool = False) -> list[BootstrappedTenant]:
        """
        Bootstrap existing tenants.

        If [force] is True, will re-bootstrap any tenants that are already bootstrapped.
        This does not change the RBAC data that already exists, but will replicate to Relations.

        This will raise an exception if bootstrapping any individual tenant would do so. Additionally, note that the
        returned list is not necessarily in the same order as the provided iterable.
        """
        tenants = set(tenants)
        lock_result = try_lock_tenants_for_bootstrap(tenants)

        to_bootstrap: list[Tenant] = []
        to_replicate: list[tuple[Tenant, TenantMapping]] = []
        bootstrap_results: list[BootstrappedTenant] = []

        for tenant in tenants:
            tenant_lock = lock_result.get(tenant)

            if tenant_lock is not None:
                if force:
                    to_replicate.append((tenant, tenant_lock.tenant_mapping))

                bootstrap_results.append(BootstrappedTenant(tenant=tenant, mapping=tenant_lock.tenant_mapping))
            else:
                to_bootstrap.append(tenant)

        # Bulk re-replicate all already-bootstrapped tenants (when force=True)
        if len(to_replicate) > 0:
            self._replicate_bootstraps(to_replicate)

        # Bootstrap all new tenants
        if len(to_bootstrap) > 0:
            bootstrap_results.extend(self._bootstrap_tenants(to_bootstrap))

        assert len(bootstrap_results) == len(tenants)
        return bootstrap_results

    def create_ungrouped_workspace(self, org_id) -> Workspace:
        """Util for creating ungrouped workspace. Can be removed once ungrouped workspace has gone."""
        tenant = Tenant.objects.get(org_id=org_id)
        default = Workspace.objects.get(tenant=tenant, type=Workspace.Types.DEFAULT)
        ungrouped_hosts, _ = Workspace.objects.get_or_create(
            tenant=tenant,
            type=Workspace.Types.UNGROUPED_HOSTS,
            name=Workspace.SpecialNames.UNGROUPED_HOSTS,
            parent=default,
            defaults={"description": Workspace.SpecialDescriptions.UNGROUPED_HOSTS},
        )

        relationship = create_relationship(
            ("rbac", "workspace"),
            str(ungrouped_hosts.id),
            ("rbac", "workspace"),
            str(default.id),
            "parent",
        )
        self._replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.CREATE_UNGROUPED_HOSTS_WORKSPACE,
                info={"org_id": tenant.org_id, "ungrouped_hosts_id": str(ungrouped_hosts.id)},
                partition_key=PartitionKey.byEnvironment(),
                add=[relationship],
            )
        )
        return ungrouped_hosts

    def update_user(
        self,
        user: User,
        upsert: bool = False,
        bootstrapped_tenant: Optional[BootstrappedTenant] = None,
        ready_tenant: bool = True,
    ) -> Optional[BootstrappedTenant]:
        """
        Bootstrap a user in a tenant.

        Create a Tenant (and bootstrap it) if it does not exist.
        If a [bootstrapped_tenant] is provided, it's assumed the Tenant already exists.

        Creates a [Principal] for the [user] if [upsert] is True. Otherwise,
        only updates the [Principal] with the user's user_id if needed.

        Returns [None] if the user is not active.
        """
        if user.org_id is None:
            raise ValueError(f"Cannot update user without org_id. username={user.username}")

        if not user.is_active:
            self._disable_user_in_tenant(user)
            return None

        bootstrapped_tenant = bootstrapped_tenant or self._get_or_bootstrap_tenant(
            user.org_id, ready_tenant, user.account
        )
        mapping = bootstrapped_tenant.mapping
        if mapping is None:
            raise ValueError(f"Expected TenantMapping but got None. org_id: {bootstrapped_tenant.tenant.org_id}")

        user.user_id = self._get_user_id(user)

        tuples_to_add = []
        tuples_to_remove = []

        # Add user to default group if not a service account
        if not user.is_service_account:
            _ensure_principal_with_user_id_in_tenant(user, bootstrapped_tenant.tenant, upsert=upsert)
            tuples_to_add, tuples_to_remove = self._default_group_tuple_edits(user, mapping)

        self._replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.EXTERNAL_USER_UPDATE,
                info={"user_id": user.user_id, "org_id": user.org_id},
                partition_key=PartitionKey.byEnvironment(),
                add=tuples_to_add,
                remove=tuples_to_remove,
            )
        )

        return bootstrapped_tenant

    def import_bulk_users(self, users: list[User], ready_tenants: bool = False):
        """
        Bootstrap multiple users in a tenant.

        Create each users' Tenant (and bootstrap it) if it does not exist.
        Updates each [Principal] with the user's user_id if Principal exists but user_id not set.

        Args:
            users (list): List of User objects to update
        """
        org_ids = set()
        for user in users:
            if not user.is_active:
                logger.info(f"User is not active. Skipping import. user_id={user.user_id} org_id={user.org_id}")
                continue
            if user.org_id is None:
                logger.warning(f"Cannot update user without org_id. Skipping. username={user.username}")
                continue
            if user.username is None:
                logger.warning(
                    "Cannot update user without username. Will bootstrap tenant but cannot update user. "
                    f"org_id={user.org_id}"
                )
            org_ids.add(user.org_id)

        bootstrapped_list = self._get_or_bootstrap_tenants(
            org_ids=org_ids,
            ready=ready_tenants,
            account_number_by_org_id={},
            bulk=True,
        )

        bootstrapped_mapping = {bootstrapped.tenant.org_id: bootstrapped for bootstrapped in bootstrapped_list}

        tuples_to_add = []
        tuples_to_remove = []
        principals_to_update = []

        # Fetch existing principals
        tenants = [bootstrapped.tenant for bootstrapped in bootstrapped_list]
        existing_principals = (
            Principal.objects.filter(Q(tenant__in=tenants) & Q(username__in=[user.username for user in users]))
            .order_by()  # remove default sort order
            .prefetch_related("tenant")
        )
        # Mapping of (org_id, username) -> principal
        # This is important because usernames are only unique by tenant
        # We don't want to match a user just by username; we could end up picking the wrong one.
        existing_principal_dict = {(p.tenant.org_id, p.username): p for p in existing_principals}

        logger.info(f"Bulk import users. found_users={len(existing_principal_dict)} total_users_in_batch={len(users)}")

        for user in users:
            if not user.is_active:
                continue
            if user.org_id is None:
                continue
            bootstrapped = bootstrapped_mapping[user.org_id]
            key = (user.org_id, user.username)
            user_id = user.user_id
            if key in existing_principal_dict:  # Principal already in rbac db
                principal = existing_principal_dict[key]
                if principal.user_id != user_id:
                    principal.user_id = user_id
                    principals_to_update.append(principal)

            mapping = bootstrapped.mapping
            if mapping is None:
                raise ValueError(f"Expected TenantMapping but got None. org_id: {bootstrapped.tenant.org_id}")

            sub_tuples_to_add, sub_tuples_to_remove = self._default_group_tuple_edits(user, mapping)
            tuples_to_add.extend(sub_tuples_to_add)
            tuples_to_remove.extend(sub_tuples_to_remove)
        # Bulk update existing principals
        if principals_to_update:
            logger.info(
                f"Add user ids. missing_user_ids={len(principals_to_update)} "
                f"found_users={len(existing_principal_dict)} total_users_in_batch={len(users)}"
            )
            Principal.objects.bulk_update(principals_to_update, ["user_id"])

        self._replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.BULK_EXTERNAL_USER_UPDATE,
                info={"num_users": len(users), "first_user_id": users[0].user_id if users else None},
                partition_key=PartitionKey.byEnvironment(),
                add=tuples_to_add,
                remove=tuples_to_remove,
            )
        )

    def _disable_user_in_tenant(self, user: User):
        """Disable a user in a tenant."""
        assert not user.is_active

        # Get tenant mapping if present but no need to create if not
        tuples_to_remove = []
        user_id = self._get_user_id(user)
        mapping: Optional[TenantMapping] = None
        principal_uuid = ""

        logger.info(
            f"Removing Principal and group membership from RBAC and Relations. user_id={user_id} org_id={user.org_id}"
        )

        try:
            mapping = TenantMapping.objects.filter(tenant__org_id=user.org_id).get()
            default_group_uuid = str(mapping.default_group_uuid)  # type: ignore
            default_admin_group_uuid = str(mapping.default_admin_group_uuid)  # type: ignore
            tuples_to_remove.append(Group.relationship_to_user_id_for_group(default_group_uuid, user_id))
            tuples_to_remove.append(Group.relationship_to_user_id_for_group(default_admin_group_uuid, user_id))
        except TenantMapping.DoesNotExist:
            logger.info(
                "No default membership to remove. There is no tenant mapping, so the tenant must not be bootstrapped."
                f"org_id={user.org_id} user_id={user_id}"
            )

        try:
            principal = Principal.objects.filter(username=user.username, tenant__org_id=user.org_id).get()
            principal_uuid = str(principal.uuid)

            for group in principal.group.all():  # type: ignore
                group.principals.remove(principal)
                # The user id might be None for the principal so we use user instead
                tuple = group.relationship_to_principal(user)
                if tuple is None:
                    raise ValueError(f"relationship_to_principal is None for user {user_id}")
                tuples_to_remove.append(tuple)

            principal.delete()  # type: ignore
        except Principal.DoesNotExist:
            logger.info(f"Could not find Principal to remove. org_id={user.org_id} user_id={user_id}")

        if not tuples_to_remove:
            return

        self._replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.EXTERNAL_USER_DISABLE,
                info={
                    "user_id": user_id,
                    "org_id": user.org_id,
                    "mapping_id": mapping.id if mapping else None,
                    "principal_uuid": principal_uuid,
                },
                partition_key=PartitionKey.byEnvironment(),
                remove=tuples_to_remove,
            )
        )

    def _get_or_bootstrap_tenant(
        self, org_id: str, ready: bool, account_number: Optional[str] = None
    ) -> BootstrappedTenant:
        bootstrapped_tenants = self._get_or_bootstrap_tenants(
            org_ids={org_id},
            ready=ready,
            account_number_by_org_id={org_id: account_number},
            bulk=False,
        )

        assert len(bootstrapped_tenants) == 1
        return bootstrapped_tenants[0]

    def _bootstrap_tenant(self, tenant: Tenant) -> BootstrappedTenant:
        bootstrapped_tenants = self._bootstrap_tenants([tenant], bulk=False)

        assert len(bootstrapped_tenants) == 1
        return bootstrapped_tenants[0]

    # Here, and in various other functions, we accept a "bulk" parameter to decide what event to use, rather than using
    # separate functions for bulk and non-bulk bootstrapping. This is to ensure that we don't accidentally let the
    # bulk and non-bulk paths diverge (as we have done in the past).

    def _replicate_bootstraps(self, tenants_with_mappings: list[tuple[Tenant, TenantMapping]], bulk: bool = True):
        """Replicate the bootstrapping of multiple tenants efficiently."""
        if not bulk and len(tenants_with_mappings) != 1:
            raise ValueError(
                f"Bulk replication is required except for exactly one tenant; got: {tenants_with_mappings}"
            )

        if not tenants_with_mappings:
            return

        # Bulk query all workspaces for all tenants at once
        all_workspaces = Workspace.objects.filter(
            tenant__in=[t for t, _ in tenants_with_mappings], type__in=[Workspace.Types.ROOT, Workspace.Types.DEFAULT]
        )

        # Group workspaces by tenant_id for fast lookup
        workspaces_by_tenant: dict[int, dict[str, Workspace]] = {}
        for ws in all_workspaces:
            if ws.tenant_id not in workspaces_by_tenant:
                workspaces_by_tenant[ws.tenant_id] = {}
            workspaces_by_tenant[ws.tenant_id][ws.type] = ws

        # Build relationships for all tenants
        all_relationships = []
        replication_entries = []

        for tenant, mapping in tenants_with_mappings:
            tenant_workspaces = workspaces_by_tenant.get(tenant.id)

            if tenant_workspaces is None:
                raise RuntimeError(f"Found no built-in workspace for tenant with pk={tenant.pk}")

            root = tenant_workspaces.get(Workspace.Types.ROOT)
            default = tenant_workspaces.get(Workspace.Types.DEFAULT)

            if (root is None) or (default is None):
                raise RuntimeError(
                    f"Missing workspaces for tenant with pk={tenant.pk} during bulk re-replication. "
                    f"Has root: {bool(root)}, has default: {bool(default)}"
                )

            all_relationships.extend(self._built_in_hierarchy_tuples(default.id, root.id, tenant.org_id))

            all_relationships.extend(
                self._bootstrap_default_access(
                    tenant,
                    mapping,
                    TenantScopeResources.for_models(
                        tenant=tenant,
                        root_workspace=root,
                        default_workspace=default,
                    ),
                )
            )

            # mypy complains about default possibly being None, even though we've check it above.
            replication_entries.append(
                _BootstrapReplicationEntry(tenant=tenant, default_workspace=default)  # type: ignore
            )

        self._send_bootstrap_events(
            replication_entries=replication_entries,
            relationships=all_relationships,
            bulk=bulk,
            forced=True,
        )

    def _query_with_default_groups(self, query_set: QuerySet) -> QuerySet:
        return query_set.prefetch_related(
            Prefetch(
                "group_set",
                queryset=Group.objects.filter(platform_default=True).order_by(),
                to_attr="_v2_bootstrap_cached_default_groups",
            )
        )

    def _fresh_tenants_with_default_groups(self, tenants: list[Tenant]) -> list[Tenant]:
        loaded = list(self._query_with_default_groups(Tenant.objects.filter(pk__in=(tenant.pk for tenant in tenants))))

        if len(loaded) != len(tenants):
            raise AssertionError(
                f"Tenant set changed concurrently. Expected {len(tenants)} tenants but got {len(loaded)}."
            )

        return loaded

    def _tenant_default_groups(self, tenant: Tenant) -> list[Group]:
        default_groups = getattr(tenant, "_v2_bootstrap_cached_default_groups", None)

        if default_groups is None:
            default_groups = list(Group.objects.filter(tenant=tenant, platform_default=True).order_by())
            tenant._v2_bootstrap_cached_default_groups = default_groups

        return default_groups

    def _get_or_bootstrap_tenants(
        self,
        org_ids: set,
        ready: bool,
        account_number_by_org_id: dict[str, Optional[str]],
        bulk: bool,
    ) -> list[BootstrappedTenant]:
        """Bootstrap list of tenants, used by import_bulk_users."""
        if not bulk and len(org_ids) != 1:
            raise ValueError("Bulk bootstrapping is required unless exactly one org_id is present.")

        # Fetch existing tenants
        existing_tenants: dict[str, Tenant] = {
            tenant.org_id: tenant for tenant in Tenant.objects.filter(org_id__in=org_ids)
        }

        tenant_locks = try_lock_tenants_for_bootstrap(existing_tenants.values())

        # An existing tenant might have been bootstrapped and already has mapping and workspaces
        tenants_to_bootstrap: list[Tenant] = []
        bootstrapped_list: list[BootstrappedTenant] = []

        for tenant in existing_tenants.values():
            lock = tenant_locks[tenant]

            if lock is None:
                tenants_to_bootstrap.append(tenant)
            else:
                logger.info(f"Tenant already bootstrapped. org_id={tenant.org_id}")
                bootstrapped_list.append(BootstrappedTenant(tenant, lock.tenant_mapping))

        # Create new tenants
        new_tenants = [
            Tenant(
                tenant_name=f"org{org_id}",
                org_id=org_id,
                ready=ready,
                account_id=account_number_by_org_id.get(org_id),
            )
            for org_id in org_ids
            if org_id not in existing_tenants
        ]

        if new_tenants:
            new_tenants = Tenant.objects.bulk_create(new_tenants)
            tenants_to_bootstrap.extend(new_tenants)

        if tenants_to_bootstrap:
            # These two strategies will result in the same structure and relationships, but the created
            # ReplicationEvent will have different metadata. We accept the flag in order to preserve the existing
            # behavior while reusing the rest of the logic.
            if bulk:
                bootstrapped_list.extend(self._bootstrap_tenants(tenants_to_bootstrap))
            else:
                # If we're not using bulk bootstrapping, there must only be one tenant, so there can only be one
                # tenant to bootstrap.
                assert len(tenants_to_bootstrap) == 1
                bootstrapped_list.append(self._bootstrap_tenant(tenants_to_bootstrap[0]))

        return bootstrapped_list

    def _bootstrap_tenants(self, tenants: list[Tenant], bulk: bool = True) -> list[BootstrappedTenant]:
        if not bulk and len(tenants) != 1:
            raise RuntimeError(f"Bulk bootstrapping is required unless exactly one tenant is present, got: {tenants}")

        bootstrapped, relationships = self._bootstrap_tenants_no_replicate(tenants)
        assert len(bootstrapped) == len(tenants)

        self._send_bootstrap_events(
            replication_entries=[
                _BootstrapReplicationEntry(tenant=b.tenant, default_workspace=b.default_workspace)  # type: ignore
                for b in bootstrapped
            ],
            relationships=relationships,
            bulk=bulk,
        )

        return bootstrapped

    def _bootstrap_tenants_no_replicate(
        self, tenants: list[Tenant]
    ) -> tuple[list[BootstrappedTenant], list[RelationTuple]]:
        # Set up workspace hierarchy for Tenant.
        if any(t.tenant_name == "public" for t in tenants):
            raise ValueError("Cannot bootstrap public tenant.")

        tenants = self._fresh_tenants_with_default_groups(tenants)
        relationships: list[RelationTuple] = []
        mappings_to_create: list[TenantMapping] = []

        default_workspaces: list[Workspace] = []
        root_workspaces: list[Workspace] = []

        for tenant in tenants:
            platform_default_groups = self._tenant_default_groups(tenant)
            kwargs = {"tenant": tenant}
            if platform_default_groups:
                group_uuid = platform_default_groups[0].uuid
                logger.info(f"Using custom default group for tenant. org_id={tenant.org_id} group_uuid={group_uuid}")
                kwargs["default_group_uuid"] = group_uuid
            mappings_to_create.append(TenantMapping(**kwargs))

            root, default, built_in_relationships = self._built_in_workspaces(tenant)

            default_workspaces.append(default)
            root_workspaces.append(root)
            relationships.extend(built_in_relationships)

        Workspace.objects.bulk_create([*default_workspaces, *root_workspaces])

        # Invalidate workspace cache for each bootstrapped tenant
        from management.cache import WORKSPACE_CACHE

        for tenant in tenants:
            if tenant.org_id:
                WORKSPACE_CACHE.delete_workspaces_for_tenant(tenant.org_id)

        mappings = TenantMapping.objects.bulk_create(mappings_to_create)
        tenant_mappings = {mapping.tenant_id: mapping for mapping in mappings}
        bootstrapped_tenants = []

        for tenant, default_workspace, root_workspace in zip(tenants, default_workspaces, root_workspaces):
            mapping = tenant_mappings[tenant.id]

            relationships.extend(
                self._bootstrap_default_access(
                    tenant,
                    mapping,
                    TenantScopeResources.for_models(
                        tenant=tenant,
                        default_workspace=default_workspace,
                        root_workspace=root_workspace,
                    ),
                )
            )

            bootstrapped_tenants.append(
                BootstrappedTenant(
                    tenant=tenant,
                    mapping=mapping,
                    default_workspace=default_workspace,
                    root_workspace=root_workspace,
                )
            )

        return bootstrapped_tenants, relationships

    def _send_bootstrap_events(
        self,
        *,
        replication_entries: list[_BootstrapReplicationEntry],
        relationships: list[RelationTuple],
        bulk: bool,
        forced: bool = False,
    ):
        """
        Replicate the relationships for bootstrapping the provided tenants.

        If bulk is false, then exactly one tenant must be provided, and the non-bulk ReplicationEventType will be used.
        """
        if not bulk and len(replication_entries) != 1:
            raise ValueError(
                f"Expected bulk replication except for exactly one tenant, but got: {replication_entries}"
            )

        if (len(replication_entries) == 0) != (len(relationships) == 0):
            raise AssertionError(
                f"Expected relationships to be empty if and only if nothing to replicate, but got: "
                f"{replication_entries=}, {relationships=}"
            )

        if len(replication_entries) == 0:
            return

        if bulk:
            event_type = ReplicationEventType.BULK_BOOTSTRAP_TENANT
            info = {"num_tenants": len(replication_entries), "first_org_id": replication_entries[0].tenant.org_id}
        else:
            event_type = ReplicationEventType.BOOTSTRAP_TENANT
            info = {"org_id": replication_entries[0].tenant.org_id}

        if forced:
            info["forced"] = True

        self._replicator.replicate(
            ReplicationEvent(
                event_type=event_type,
                info=info,
                partition_key=PartitionKey.byEnvironment(),
                add=relationships,
            )
        )

        for entry in replication_entries:
            # We always use the STANDARD stream here; the BULK stream is intended for things like "replicating every
            # default workspace that exists". The bulk parameter here is just used for determining which type of
            # replication event to send.
            self._replicator.replicate_workspace(
                make_workspace_event(
                    workspace=entry.default_workspace, event_type=ReplicationEventType.CREATE_WORKSPACE
                ),
                WorkspaceEventStream.STANDARD,
            )

    def _default_group_tuple_edits(self, user: User, mapping) -> tuple[list[RelationTuple], list[RelationTuple]]:
        """Get the tuples to add and remove for a user."""
        tuples_to_add = []
        tuples_to_remove = []
        user_id = self._get_user_id(user)

        tuples_to_add.append(Group.relationship_to_user_id_for_group(str(mapping.default_group_uuid), user_id))

        # Add user to admin group if admin
        if user.admin:
            tuples_to_add.append(
                Group.relationship_to_user_id_for_group(str(mapping.default_admin_group_uuid), user_id)
            )
        else:
            # If not admin, ensure they are not in the admin group
            # (we don't know what the previous state was)
            tuples_to_remove.append(
                Group.relationship_to_user_id_for_group(str(mapping.default_admin_group_uuid), user_id)
            )

        return tuples_to_add, tuples_to_remove

    def _built_in_hierarchy_tuples(self, default_workspace_id, root_workspace_id, org_id) -> List[RelationTuple]:
        """Create tuples for default->root workspace parents and tenant->platform."""
        tenant_id = Tenant.org_id_to_tenant_resource_id(org_id=org_id)

        return [
            create_relationship(
                ("rbac", "workspace"),
                str(default_workspace_id),
                ("rbac", "workspace"),
                str(root_workspace_id),
                "parent",
            ),
            create_relationship(
                ("rbac", "workspace"),
                str(root_workspace_id),
                ("rbac", "tenant"),
                tenant_id,
                "tenant",
            ),
            create_relationship(
                ("rbac", "tenant"),
                tenant_id,
                ("rbac", "platform"),
                settings.ENV_NAME,
                "platform",
            ),
        ]

    def _bootstrap_default_access(
        self,
        tenant: Tenant,
        mapping: TenantMapping,
        scope_resources: TenantScopeResources,
    ) -> List[RelationTuple]:
        """
        Bootstrap default access for a tenant's users and admins.

        Creates role bindings between the tenant's default workspace, default groups, and system policies.
        """
        tuples_to_add: List[RelationTuple] = []

        # Add default role binding IFF there is no custom default access for the tenant

        # NOTE: This logic is prone to write skew: the platform group for the tenant may be created concurrently.
        # Care must be taken to prevent this.
        # Currently, when this default group is created (in group/definer.py) we:
        # 1. Check for the existence of a tenant mapping. If exists, use that.
        #    No race because if it already exists, this process must've already happened.
        # 2. If tenant mapping does not exist, create it via this same bootstrap process.
        #    Due to unique constraint, if this happens concurrently from another input (e.g. user import),
        #    one will rollback, serializing the group creation with user import on next retry.
        if not self._tenant_default_groups(tenant):
            try:
                tuples_to_add.extend(
                    default_role_binding_tuples(
                        tenant_mapping=mapping,
                        target_resources=scope_resources,
                        access_type=DefaultAccessType.USER,
                        policy_service=self._policy_service,
                    )
                )
            except DefaultGroupNotAvailableError:
                logger.warning("No platform default role found for public tenant. Default access will not be set up.")
        else:
            logger.info(
                f"Not setting up default access for tenant with customized default group. org_id={tenant.org_id}"
            )

        # Admin role binding is not customizable
        try:
            tuples_to_add.extend(
                default_role_binding_tuples(
                    tenant_mapping=mapping,
                    target_resources=scope_resources,
                    access_type=DefaultAccessType.ADMIN,
                    policy_service=self._policy_service,
                )
            )
        except DefaultGroupNotAvailableError:
            logger.warning("No admin default role found for public tenant. Default access will not be set up.")

        return tuples_to_add

    def _built_in_workspaces(self, tenant: Tenant) -> tuple[Workspace, Workspace, list[RelationTuple]]:
        relationships = []

        root = Workspace(
            tenant=tenant,
            type=Workspace.Types.ROOT,
            name=Workspace.SpecialNames.ROOT,
            description=Workspace.SpecialDescriptions.ROOT,
        )
        default = Workspace(
            parent_id=root.id,
            tenant=tenant,
            type=Workspace.Types.DEFAULT,
            name=Workspace.SpecialNames.DEFAULT,
            description=Workspace.SpecialDescriptions.DEFAULT,
        )

        root_workspace_id = root.id
        default_workspace_id = default.id

        relationships.extend(self._built_in_hierarchy_tuples(default_workspace_id, root_workspace_id, tenant.org_id))

        return root, default, relationships

    def create_workspace_relationships(self, pairs):
        """
        Util for bulk creating workspace relationships based on pairs.

        Input: pairs - List of tuples of (resource_id, subject_id)
        """
        relationships = []
        for pair in pairs:
            relationship = create_relationship(
                ("rbac", "workspace"),
                str(pair[0]),
                ("rbac", "workspace"),
                str(pair[1]),
                "parent",
            )
            relationships.append(relationship)
        self._replicator.replicate(
            ReplicationEvent(
                event_type=ReplicationEventType.WORKSPACE_IMPORT,
                info={},
                partition_key=PartitionKey.byEnvironment(),
                add=relationships,
            )
        )
