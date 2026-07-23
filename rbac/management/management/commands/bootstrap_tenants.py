"""Command to handle tenant bootstrapping."""

import enum
import itertools
import logging
from typing import Optional

from django.core.management import BaseCommand, CommandError
from django.db import transaction
from django.db.models import QuerySet
from management.relation_replicator.outbox_replicator import OutboxReplicator
from management.tenant_service import V2TenantBootstrapService
from management.tenant_service.tenant_service import BootstrappedTenant

from api.models import Tenant

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class _BootstrapError(enum.IntEnum):
    NO_SUCH_TENANT = 1
    FAILED = 2


type _BootstrapResult = BootstrappedTenant | _BootstrapError
type _BulkBootstrapResult = dict[Tenant, _BootstrapResult]


def _try_bulk_bootstrap(
    bootstrap_service: V2TenantBootstrapService,
    raw_tenants: set[Tenant],
    force: bool,
) -> Optional[_BulkBootstrapResult]:
    try:
        with transaction.atomic():
            tenants = Tenant.objects.select_for_update().filter(pk__in=(t.pk for t in raw_tenants))

            # A tenant has vanished. Don't try to figure out which; just give up instead.
            if len(tenants) != len(raw_tenants):
                logger.info("Could not find a tenant about to be bootstrapped; refusing to bulk bootstrap.")
                return None

            logger.info(f"Bootstrapping {len(raw_tenants)} tenants...")
            result = bootstrap_service.bootstrap_tenants(tenants, force=force)

            assert len(result) == len(tenants)
            return {b.tenant: b for b in result}
    except Exception as e:
        logger.warning(f"Failed to bulk bootstrap tenants: pks={[t.pk for t in raw_tenants]}.", exc_info=e)
        return None


def _single_bootstrap_with_retry(
    bootstrap_service: V2TenantBootstrapService,
    raw_tenant: Tenant,
    force: bool,
) -> _BootstrapResult:
    max_attempts = 5

    for attempt in range(max_attempts):
        with transaction.atomic():
            tenant = Tenant.objects.select_for_update().filter(pk=raw_tenant.pk).first()

            if tenant is None:
                logger.info(f"Tenant (pk={raw_tenant.pk!r}) no longer exists; not bootstrapping.")
                return _BootstrapError.NO_SUCH_TENANT

            tenant_desc = f"(pk={tenant.pk!r}, org_id={tenant.org_id!r})"

            try:
                logger.info(f"Bootstrapping tenant {tenant_desc}, attempt {attempt + 1}/{max_attempts}...")
                bootstrapped = bootstrap_service.bootstrap_tenant(tenant, force=force)

                logger.info(f"Successfully bootstrapped tenant {tenant_desc}.")
                return bootstrapped
            except Exception as e:
                logger.error(f"Failed to bootstrap tenant {tenant_desc}!", exc_info=e)

    logger.error(f"Could not bootstrap tenant (pk={raw_tenant.pk!r}) after {max_attempts} attempts.")
    return _BootstrapError.FAILED


def _bulk_bootstrap_with_retry(
    bootstrap_service: V2TenantBootstrapService,
    raw_tenants: set[Tenant],
    force: bool,
) -> dict[Tenant, BootstrappedTenant | _BootstrapError]:
    bulk_result = _try_bulk_bootstrap(
        bootstrap_service=bootstrap_service,
        raw_tenants=raw_tenants,
        force=force,
    )

    if bulk_result is not None:
        return bulk_result

    logger.info(f"Individually bootstrapping {len(raw_tenants)} tenants.")
    results = {}

    for raw_tenant in raw_tenants:
        results[raw_tenant] = _single_bootstrap_with_retry(
            bootstrap_service=bootstrap_service,
            raw_tenant=raw_tenant,
            force=force,
        )

    return results


class Command(BaseCommand):
    """Command for manually bootstrapping tenants and re-replicating existing bootstraps."""

    help = "Bootstrap tenants or re-replicate existing tenant bootstraps."

    def add_arguments(self, parser):
        """Parse command arguments."""
        parser.add_argument(
            "--all",
            action="store_true",
            help="bootstrap all tenants",
        )

        parser.add_argument(
            "--org-id",
            action="append",
            default=[],
            help="bootstrap tenant with specific org ID (repeatable)",
        )

        parser.add_argument(
            "--force",
            action="store_true",
            help="re-replicate bootstrap relations for bootstrapped tenants",
        )

    def handle(self, **options):
        """Run the command."""
        use_all = options["all"]

        requested_org_ids: set[str] = set(options["org_id"])
        use_org_ids = bool(requested_org_ids)

        if (not use_all and not use_org_ids) or (use_all and use_org_ids):
            raise CommandError(
                "Must either specify --all to bootstrap all tenants or use --org-id to "
                "specify one or more tenants to bootstrap."
            )

        if use_all:
            assert not use_org_ids
            logger.info("Bootstrap of all tenants requested.")
            base_query: QuerySet = Tenant.objects.all()
        else:
            assert use_org_ids
            logger.info(f"Bootstrapping of the tenants with the following org IDs requested: {requested_org_ids}")
            base_query: QuerySet = Tenant.objects.filter(org_id__in=requested_org_ids)

        force = options["force"]
        bootstrap_service = V2TenantBootstrapService(replicator=OutboxReplicator())

        query = base_query.exclude(tenant_name="public")
        estimate = query.count()

        logger.info(f"About to bootstrap an estimated {estimate} tenants...")
        logger.info(f"Running with {force=}.")

        successful_org_ids = set[str]()  # Only populated if use_org_ids is true.
        missing_org_ids = set[str]()
        failed_org_ids = set[str]()

        tenants_seen = 0

        # These are "raw" because we haven't locked anything, and the tenant could vanish out from under us.
        #
        # We use a batch size of 40 because there is a number limit relationships of 1 k:
        # https://authzed.com/docs/spicedb/ops/data/bulk-operations At time of
        # writing (2025-11-04), there are 21 relations per bootstrapped tenant at most (when there is no custom
        # default group). This value may need to be updated if this script is used in the future.
        for raw_tenants in itertools.batched(query.iterator(), 40):
            logger.info(f"Bootstrapping tenant {tenants_seen + 1}-{tenants_seen + len(raw_tenants)}/{estimate}...")

            bulk_result = _bulk_bootstrap_with_retry(
                bootstrap_service=bootstrap_service,
                raw_tenants=set(raw_tenants),
                force=force,
            )

            for raw_tenant, tenant_result in bulk_result.items():

                if isinstance(tenant_result, BootstrappedTenant):
                    if use_org_ids:
                        successful_org_ids.add(tenant_result.tenant.org_id)
                else:
                    if tenant_result == _BootstrapError.NO_SUCH_TENANT:
                        missing_org_ids.add(raw_tenant.org_id)
                    elif tenant_result == _BootstrapError.FAILED:
                        failed_org_ids.add(raw_tenant.org_id)
                    else:
                        raise ValueError(f"Unexpected result: {tenant_result}")

            tenants_seen += len(raw_tenants)

        if missing_org_ids:
            logger.warning(
                f"The following org IDs were found but then disappeared before they could be bootstrapped: "
                f"{missing_org_ids}"
            )

        # Always exit with abnormal status if we failed to bootstrap a tenant.
        if failed_org_ids:
            failed_message = f"The following org IDs could not be bootstrapped: {failed_org_ids}"
            logger.warning(failed_message)
            raise CommandError(failed_message, returncode=1)

        if use_org_ids and (successful_org_ids != requested_org_ids):
            not_found = requested_org_ids.difference(successful_org_ids)

            raise CommandError(
                f"The following org IDs were requested to be bootstrapped but were not found: {not_found}",
                returncode=1,
            )
