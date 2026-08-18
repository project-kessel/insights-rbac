#
# Copyright 2019 Red Hat, Inc.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Ensure an org tenant and principal exist; optionally grant admin-default roles."""

import logging

from django.core.cache import cache
from django.core.management import BaseCommand, CommandError, call_command
from django.db import transaction

from api.models import Tenant
from management.models import Group, Policy, Principal, Role

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

_DEFAULT_ADMIN_GROUP_NAME = "Default admin access"
_DEFAULT_ADMIN_GROUP_DESCRIPTION = (
    "This group contains the roles that all org admin users inherit by default. "
    "Adding or removing roles in this group will affect permissions for all org admin users in your org."
)
_DEFAULT_ADMIN_POLICY_NAME = "Default admin access policy"


class Command(BaseCommand):
    """Create or update an org tenant, principal, and optional admin group membership."""

    help = (
        "Create an org tenant and principal. With --admin, add the principal to the "
        "tenant admin-default group for the given --application values."
    )

    def add_arguments(self, parser):
        """Add command arguments."""
        parser.add_argument("--username", required=True, help="Principal username")
        parser.add_argument("--org-id", required=True, help="Organization ID")
        parser.add_argument("--account-number", required=True, help="Account number for tenant_name acct{N}")
        parser.add_argument(
            "--application",
            action="append",
            dest="applications",
            metavar="APP",
            help=(
                "Limit admin roles to these permission applications (repeatable). "
                "When --admin is set and this is omitted, all admin_default roles are used."
            ),
        )
        parser.add_argument(
            "--admin",
            action="store_true",
            help="Add the principal to the tenant admin-default group with matching roles",
        )
        parser.add_argument(
            "--admin-group-name",
            default=_DEFAULT_ADMIN_GROUP_NAME,
            help="Name of the per-org admin group to create or update (default: RBAC seed name)",
        )
        parser.add_argument(
            "--admin-group-description",
            default=_DEFAULT_ADMIN_GROUP_DESCRIPTION,
            help="Description for the admin group when it is first created",
        )
        parser.add_argument(
            "--admin-policy-name",
            default=_DEFAULT_ADMIN_POLICY_NAME,
            help="Name of the policy linking admin roles to the admin group",
        )

    def handle(self, *args, **options):
        """Handle command execution."""
        username = options["username"]
        org_id = options["org_id"]
        account_number = options["account_number"]
        applications = options.get("applications") or []
        grant_admin = options["admin"]
        admin_group_name = options["admin_group_name"]
        admin_group_description = options["admin_group_description"]
        admin_policy_name = options["admin_policy_name"]

        if grant_admin and not applications:
            logger.info("No --application specified; using all admin_default roles")

        with transaction.atomic():
            public_tenant = Tenant.objects.get(tenant_name="public")
            tenant, created = Tenant.objects.get_or_create(
                org_id=org_id,
                defaults={"tenant_name": "acct" + account_number, "ready": True},
            )
            logger.info(
                f"{'Created' if created else 'Existing'} tenant for org_id={org_id} (name={tenant.tenant_name})"
            )

            principal, principal_created = Principal.objects.get_or_create(
                username=username,
                tenant=tenant,
                defaults={"type": Principal.Types.USER},
            )
            logger.info(
                f"{'Created' if principal_created else 'Existing'} principal {username!r} in tenant {tenant.tenant_name}"
            )

            if grant_admin:
                admin_roles = Role.objects.filter(
                    admin_default=True,
                    tenant=public_tenant,
                )
                if applications:
                    admin_roles = admin_roles.filter(
                        access__permission__application__in=applications,
                    )
                admin_roles = admin_roles.distinct().order_by("name")
                if not admin_roles.exists():
                    raise CommandError(
                        "No admin_default roles found for applications "
                        f"{applications!r}; run migrations and seeds first"
                    )

                group, group_created = Group.objects.get_or_create(
                    name=admin_group_name,
                    tenant=tenant,
                    defaults={
                        "admin_default": True,
                        "description": admin_group_description,
                        "system": True,
                    },
                )
                if not group_created and not group.admin_default:
                    group.admin_default = True
                    group.save(update_fields=["admin_default"])
                if group_created:
                    logger.info(f"Created admin-default group {group.name!r}")
                else:
                    logger.info(f"Using existing admin-default group {group.name!r}")

                policy, _ = Policy.objects.get_or_create(
                    name=admin_policy_name,
                    tenant=tenant,
                    group=group,
                    defaults={"system": True},
                )
                policy.roles.set(admin_roles)
                group.principals.add(principal)

                role_names = list(admin_roles.values_list("name", flat=True))
                logger.info(f"Granted admin roles {role_names} to {username!r} in org {org_id}")

        cache.clear()
        logger.info(f"Purged access cache after ensure_user for org_id={org_id}")

        call_command("bootstrap_tenants", "--org-id", org_id, "--force", verbosity=options.get("verbosity", 1))
