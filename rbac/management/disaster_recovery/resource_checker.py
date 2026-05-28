"""Check resource existence in the RBAC database for disaster recovery reconciliation."""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from api.models import Tenant
from management.group.model import Group
from management.principal.model import Principal
from management.relation_replicator.types import RelationTuple
from management.role.model import Role
from management.role_binding.model import RoleBinding
from management.workspace.model import Workspace

logger = logging.getLogger(__name__)


def _strip_domain_prefix(resource_id: str) -> str:
    parts = resource_id.rsplit("/", 1)
    return parts[-1] if len(parts) > 1 else resource_id


@dataclass(frozen=True)
class ResourceTypeConfig:
    model: type
    id_field: str
    id_transform: Callable[[str], str] | None = None


RESOURCE_TYPE_REGISTRY: dict[str, ResourceTypeConfig] = {
    "workspace": ResourceTypeConfig(model=Workspace, id_field="id"),
    "role": ResourceTypeConfig(model=Role, id_field="uuid"),
    "group": ResourceTypeConfig(model=Group, id_field="uuid"),
    "principal": ResourceTypeConfig(model=Principal, id_field="user_id", id_transform=_strip_domain_prefix),
    "role_binding": ResourceTypeConfig(model=RoleBinding, id_field="uuid"),
    "tenant": ResourceTypeConfig(model=Tenant, id_field="org_id", id_transform=_strip_domain_prefix),
}


def check_resources_exist(tuples: list[RelationTuple]) -> dict[tuple[str, str], bool]:
    """Check which resources referenced by tuples exist in the RBAC database.

    Returns a dict mapping (resource_type_name, resource_id) -> exists.
    Groups queries by resource type for efficiency (one query per type).
    Unknown types return True (safe default = skip corrective action).
    """
    ids_by_type: dict[str, set[str]] = {}

    for t in tuples:
        resource_type_name = t.resource.type.name
        ids_by_type.setdefault(resource_type_name, set()).add(t.resource.id)

    result: dict[tuple[str, str], bool] = {}

    for type_name, raw_ids in ids_by_type.items():
        config = RESOURCE_TYPE_REGISTRY.get(type_name)
        if config is None:
            logger.warning("Unknown resource type '%s', treating as existing (safe default)", type_name)
            for raw_id in raw_ids:
                result[(type_name, raw_id)] = True
            continue

        if config.id_transform:
            id_mapping = {raw_id: config.id_transform(raw_id) for raw_id in raw_ids}
        else:
            id_mapping = {raw_id: raw_id for raw_id in raw_ids}

        lookup_ids = set(id_mapping.values())
        existing_ids = set(
            config.model.objects.filter(**{f"{config.id_field}__in": lookup_ids}).values_list(
                config.id_field, flat=True
            )
        )
        existing_ids = {str(eid) for eid in existing_ids}

        for raw_id, lookup_id in id_mapping.items():
            result[(type_name, raw_id)] = str(lookup_id) in existing_ids

    return result
