"""Command to fix orphan relations in all tenants."""

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
import dataclasses
import datetime
import enum
import itertools
import logging
from collections.abc import Iterable
from typing import Optional

from django.core.management import BaseCommand, CommandError
from django.db.models import Q, QuerySet
from internal.migrations.remove_orphan_relations import cleanup_tenant_orphan_bindings
from management.audit_log.model import AuditLog
from management.group.model import Group
from management.role.model import Role
from management.role.v2_model import RoleV2
from management.role_binding.model import RoleBinding
from management.workspace.model import Workspace

from api.cross_access.model import CrossAccountRequest
from api.models import Tenant

logger = logging.getLogger(__name__)

_abort_threshold = 10


class _StopReason(enum.Enum):
    SUCCESS = "success"
    ABORTED = "aborted"


@dataclasses.dataclass
class _MigrateResult:
    stop_reason: _StopReason
    success_count: int
    modified_count: int
    failed_orgs: frozenset[str]


class Command(BaseCommand):
    """Command to fix orphan relations in all tenants."""

    help = """
    Remove orphan relations from all tenants.

    This currently handles:
    * Orphaned relations from role bindings.
    * Orphaned parent relations from deleted workspaces.
    * Incorrect parent relations from existing workspaces.
    """

    def add_arguments(self, parser):
        """Add arguments for the command."""
        parser.add_argument(
            "--all",
            action="store_true",
            help="remove orphan relations for all tenants",
        )

        parser.add_argument(
            "--tenant-limit",
            type=int,
            help="maximum number of tenants to process",
        )

    def _tenants_query(self) -> QuerySet[Tenant]:
        base = Tenant.objects.exclude(tenant_name="public").filter(org_id__isnull=False)

        # We consider only the tenants where it is known that meaningful operations have taken place. Empty tenants
        # are presumed not to have orphans (since there is no known way this could have occurred).
        #
        # We specifically include tenants with AuditLog entries because they could have had roles or groups that were
        # incorrectly processed (e.g. by a binding scope migration that was run incorrectly) but were since deleted
        # (which, in that case, would have resulted in role bindings that exist locally but not in Kessel being
        # "deleted" from Kessel, while the role bindings that *did* still exist in Kessel remained unaffected). The
        # deletion of the roles/groups would have resulted in AuditLogs being created (at least since June 2024,
        # which is before the source of any known issues), so we can use that to determine which tenants to process.
        interesting_tenants = base.filter(
            Q(id__in=(Group.objects.all().values_list("tenant_id", flat=True).distinct()))
            | Q(id__in=(Role.objects.all().values_list("tenant_id", flat=True).distinct()))
            | Q(id__in=(RoleV2.objects.all().values_list("tenant_id", flat=True).distinct()))
            | Q(id__in=(RoleBinding.objects.all().values_list("tenant_id", flat=True).distinct()))
            | Q(
                id__in=(
                    Workspace.objects.filter(type=Workspace.Types.STANDARD)
                    .values_list("tenant_id", flat=True)
                    .distinct()
                )
            )
            | Q(org_id__in=(CrossAccountRequest.objects.values_list("target_org", flat=True).distinct()))
            | Q(id__in=(AuditLog.objects.values_list("tenant_id", flat=True).distinct()))
        ).distinct()

        logger.info(
            f"About to load ~{interesting_tenants.count()} with an extant role, group, standard workspace, "
            f"role binding, or audit log entry."
        )

        return interesting_tenants

    @staticmethod
    def _try_migrate(tenants: Iterable[Tenant], estimated_count: int) -> _MigrateResult:
        stop_reason = _StopReason.SUCCESS

        success_count = 0
        modified_count = 0
        failed_orgs = []

        consecutive_failures = 0

        for index, tenant in enumerate(tenants):
            start_time = datetime.datetime.now(datetime.timezone.utc)
            modified = False
            failed = False

            logger.info(
                f"Beginning migration of tenant {index + 1}/~{estimated_count} with org_id={tenant.org_id!r} "
                f"at {start_time}"
            )

            try:
                result = cleanup_tenant_orphan_bindings(org_id=tenant.org_id)

                if "error" in result:
                    error = result["error"]
                    logger.error(
                        f"Failed to remove orphan relations for tenant with org_id={tenant.org_id!r}: {error}"
                    )
                    failed = True

                if result.get("cleanup", {}).get("relations_removed_count", 0) > 0:
                    modified = True
            except Exception as e:
                logger.error(f"Failed to remove orphan relations for tenant with org_id={tenant.org_id!r}", exc_info=e)
                failed = True

            end_time = datetime.datetime.now(datetime.timezone.utc)

            if failed:
                failed_orgs.append(tenant.org_id)
                consecutive_failures += 1

                logger.info(
                    f"Failed migration of tenant with org_id={tenant.org_id!r} at {end_time}; "
                    f"took {end_time - start_time}."
                )
            else:
                logger.info(
                    f"Done with migration of tenant with org_id={tenant.org_id!r} at {end_time}; "
                    f"modified={'true' if modified else 'false'}; took {end_time - start_time}."
                )

                success_count += 1
                consecutive_failures = 0

                if modified:
                    modified_count += 1

            if consecutive_failures >= _abort_threshold:
                stop_reason = _StopReason.ABORTED
                logger.error(f"Aborting after {consecutive_failures} consecutive tenants failed.")
                break

        return _MigrateResult(
            stop_reason=stop_reason,
            success_count=success_count,
            modified_count=modified_count,
            failed_orgs=frozenset(failed_orgs),
        )

    def _limited_tenants(self, limit: Optional[int]) -> tuple[Iterable[Tenant], int]:
        base_query = self._tenants_query()
        base_count = base_query.count()

        if limit is None:
            return base_query.iterator(), base_count

        return itertools.islice(base_query.iterator(), limit), min(base_count, limit)

    def handle(self, *args, **options):
        """Execute the command."""
        limit: Optional[int] = options["tenant_limit"]

        if (not options["all"]) and (limit is None):
            raise CommandError(
                "Must pass --all or --tenant-limit to specify how many tenants to process.",
                returncode=2,
            )

        if (limit is not None) and (limit <= 0):
            raise CommandError("Limit must be positive.", returncode=2)

        tenants, tenant_count = self._limited_tenants(limit=limit)

        logger.info(f"About to remove orphan relations for ~{tenant_count} tenants.")

        result = self._try_migrate(tenants=tenants, estimated_count=tenant_count)

        logger.info(
            f"Successfully removed orphan relations for {result.success_count} tenants, "
            f"of which {result.modified_count} were modified."
        )

        if len(result.failed_orgs) > 0:
            raise CommandError(
                f"Failed to remove orphan relations tenants with the following org_ids: {result.failed_orgs}",
                returncode=1,
            )

        if result.stop_reason != _StopReason.SUCCESS:
            raise CommandError(f"Stopped for a reason other than success: {result.stop_reason.name}", returncode=1)
