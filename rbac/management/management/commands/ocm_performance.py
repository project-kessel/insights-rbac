# 1 request for each tenant (to get tenant's groups)
# 1 request for each group (to get roles)
# 1 request for each tenant (to get the principles)
# 1 request for each principle in each org (to get the principles' groups)

# Populate the database with a large number of tenants, groups, principles, and roles
# ~20k requests = ~2k tenants ~10 principle per tenant
# optional req: 2 groups per principle, 5 roles per group with 10 permissions each
"""Baseline tests for OCM RBAC performance."""

from django.core.management.base import BaseCommand
from tests.performance.test_performance_concurrent import (
    test_full_sync,
    test_group_roles,
    test_principals_groups,
    test_principals_roles,
    test_tenant_groups,
    test_tenant_roles,
)
from tests.performance.test_performance_util import setUp, tearDown


class Command(BaseCommand):
    """Command to setup, run, and teardown ocm performance tests."""

    help = """
    Run the OCM performance tests. If running locally,
    run the setup command first to populate the database.

    Usage:
        python manage.py command ocm_performance [setup|test|teardown]
    """

    def add_arguments(self, parser):
        """Parse command arguments."""
        parser.add_argument("mode", type=str, nargs="?", default="test", help="Choice of setup, test, or teardown")

    def handle(self, **options):
        """Run the command."""
        mode = options["mode"]
        if mode == "setup":
            setUp()
        elif mode == "teardown":
            tearDown()
        elif mode == "test":
            # run the ocm performance tests
            test_full_sync()
            test_tenant_groups()
            test_tenant_roles()
            test_group_roles()
            test_principals_roles()
            test_principals_groups()
        else:
            print("Invalid mode. Please choose from setup, test, or teardown.")
