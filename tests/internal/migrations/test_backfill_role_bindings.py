#
# Copyright 2026 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
"""Tests for the default role binding backfill."""

from unittest.mock import patch

from django.core.management import CommandError, call_command
from internal.migrations.backfill_role_bindings import (
    ABORT_THRESHOLD,
    BackfillResult,
    backfill_default_role_bindings,
)
from management.group.definer import seed_group
from management.group.model import Group
from management.group.platform import DefaultGroupNotAvailableError, GlobalPolicyIdService
from management.permission.scope_service import CONCRETE_SCOPES
from management.role.definer import seed_roles
from management.role_binding.model import RoleBinding
from management.role_binding.service import RoleBindingService
from management.tenant_mapping.model import DefaultAccessType, TenantMapping
from management.tenant_mapping.v2_activation import set_v2_opt_in_state

from api.models import Tenant
from tests.identity_request import IdentityRequest
from tests.management.role.test_dual_write import RbacFixture

CREATE_ALL_DEFAULT_BINDINGS = "management.role_binding.service.RoleBindingService._create_all_default_bindings"


class BackfillDefaultRoleBindingsTests(IdentityRequest):
    """Tests for backfill_default_role_bindings."""

    def setUp(self):
        GlobalPolicyIdService.clear_shared()
        Tenant._public_tenant = None

        # Platform roles and public default groups are required for default binding creation.
        seed_roles()
        seed_group()

        self.fixture = RbacFixture()

    def _opted_in_tenant(self, org_id: str) -> TenantMapping:
        """Opt a new tenant into V2 without creating its default bindings.

        Mirrors the state of tenants that opted in before RHCLOUD-50167 made binding creation eager.
        """
        bootstrapped = self.fixture.new_tenant(org_id=org_id)

        with patch(CREATE_ALL_DEFAULT_BINDINGS):
            set_v2_opt_in_state(bootstrapped.tenant, True)

        return bootstrapped.mapping

    def _count_default_bindings(self, mapping: TenantMapping, access_type: DefaultAccessType) -> int:
        binding_uuids = [mapping.default_role_binding_uuid_for(access_type, s) for s in CONCRETE_SCOPES]
        return RoleBinding.objects.filter(uuid__in=binding_uuids).count()

    def test_creates_bindings_for_opted_in_tenant(self):
        """An opted-in tenant without default bindings gets them created."""
        mapping = self._opted_in_tenant("backfill-org")

        result = backfill_default_role_bindings()

        self.assertEqual(result.tenants_processed, 1)
        self.assertEqual(result.tenants_backfilled, 1)
        self.assertEqual(result.already_complete, 0)
        self.assertEqual(result.incomplete, 0)
        self.assertEqual(result.errors, 0)
        self.assertEqual(self._count_default_bindings(mapping, DefaultAccessType.ADMIN), len(CONCRETE_SCOPES))
        self.assertEqual(self._count_default_bindings(mapping, DefaultAccessType.USER), len(CONCRETE_SCOPES))

    def test_skips_tenant_that_is_not_opted_in(self):
        """A bootstrapped but never opted-in tenant is left alone."""
        bootstrapped = self.fixture.new_tenant(org_id="v1-org")

        result = backfill_default_role_bindings()

        self.assertEqual(result.tenants_processed, 0)
        self.assertEqual(self._count_default_bindings(bootstrapped.mapping, DefaultAccessType.ADMIN), 0)

    def test_already_complete_tenant_is_not_recreated(self):
        """A tenant whose default bindings all exist is counted as complete and skipped."""
        self._opted_in_tenant("backfill-complete-org")
        backfill_default_role_bindings()

        with patch.object(RoleBindingService, "create_default_bindings") as mock_create:
            result = backfill_default_role_bindings()

        mock_create.assert_not_called()
        self.assertEqual(result.tenants_processed, 1)
        self.assertEqual(result.already_complete, 1)
        self.assertEqual(result.tenants_backfilled, 0)

    def test_is_idempotent(self):
        """Running the backfill twice leaves exactly one set of default bindings."""
        mapping = self._opted_in_tenant("backfill-idempotent-org")

        backfill_default_role_bindings()
        backfill_default_role_bindings()

        self.assertEqual(self._count_default_bindings(mapping, DefaultAccessType.ADMIN), len(CONCRETE_SCOPES))
        self.assertEqual(self._count_default_bindings(mapping, DefaultAccessType.USER), len(CONCRETE_SCOPES))

    def test_tenant_with_custom_default_group_gets_admin_bindings_only(self):
        """A custom default group replaces platform default access, so USER bindings are skipped."""
        mapping = self._opted_in_tenant("backfill-custom-group-org")
        Group.objects.create(name="Custom default", platform_default=True, tenant=mapping.tenant)

        result = backfill_default_role_bindings()

        self.assertEqual(result.tenants_backfilled, 1)
        self.assertEqual(self._count_default_bindings(mapping, DefaultAccessType.ADMIN), len(CONCRETE_SCOPES))
        self.assertEqual(self._count_default_bindings(mapping, DefaultAccessType.USER), 0)

    def test_write_activated_tenant_is_processed_once(self):
        """A tenant with both opt-in and write-activation timestamps is processed a single time."""
        mapping = self._opted_in_tenant("backfill-activated-org")
        TenantMapping.objects.filter(pk=mapping.pk).update(v2_write_activated_at=mapping.v2_opted_in_at)

        result = backfill_default_role_bindings()

        self.assertEqual(result.tenants_processed, 1)
        self.assertEqual(result.tenants_backfilled, 1)

    def test_unseeded_platform_roles_count_as_incomplete(self):
        """A tenant whose platform roles are missing is reported as incomplete, not as an error."""
        mapping = self._opted_in_tenant("backfill-unseeded-org")

        with patch(CREATE_ALL_DEFAULT_BINDINGS, side_effect=DefaultGroupNotAvailableError()):
            result = backfill_default_role_bindings()

        self.assertEqual(result.tenants_processed, 1)
        self.assertEqual(result.incomplete, 1)
        self.assertEqual(result.tenants_backfilled, 0)
        self.assertEqual(result.errors, 0)
        self.assertEqual(self._count_default_bindings(mapping, DefaultAccessType.ADMIN), 0)

    def test_failure_for_one_tenant_does_not_abort_the_run(self):
        """A tenant that raises is counted as an error and the remaining tenants still get bindings."""
        failing = self._opted_in_tenant("backfill-failing-org")
        healthy = self._opted_in_tenant("backfill-healthy-org")

        real_create_all = RoleBindingService._create_all_default_bindings

        def create_all(service, mapping, skip_user_bindings=False):
            if service.tenant.org_id == failing.tenant.org_id:
                raise ValueError("boom")
            return real_create_all(service, mapping, skip_user_bindings=skip_user_bindings)

        with patch(CREATE_ALL_DEFAULT_BINDINGS, autospec=True, side_effect=create_all):
            result = backfill_default_role_bindings()

        self.assertEqual(result.tenants_processed, 2)
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.tenants_backfilled, 1)
        self.assertEqual(self._count_default_bindings(failing, DefaultAccessType.ADMIN), 0)
        self.assertEqual(self._count_default_bindings(healthy, DefaultAccessType.ADMIN), len(CONCRETE_SCOPES))

    def test_org_ids_filter_restricts_the_backfill(self):
        """Only the named orgs are processed."""
        included = self._opted_in_tenant("backfill-included-org")
        excluded = self._opted_in_tenant("backfill-excluded-org")

        result = backfill_default_role_bindings(org_ids=["backfill-included-org"])

        self.assertEqual(result.tenants_processed, 1)
        self.assertEqual(self._count_default_bindings(included, DefaultAccessType.ADMIN), len(CONCRETE_SCOPES))
        self.assertEqual(self._count_default_bindings(excluded, DefaultAccessType.ADMIN), 0)

    def test_unknown_org_id_processes_nothing(self):
        """An org ID with no matching tenant mapping yields an all-zero summary, not an error."""
        self._opted_in_tenant("backfill-known-org")

        result = backfill_default_role_bindings(org_ids=["no-such-org"])

        self.assertEqual(result.tenants_processed, 0)
        self.assertEqual(result.tenants_backfilled, 0)
        self.assertEqual(result.errors, 0)

    def test_missing_user_bindings_are_restored(self):
        """A tenant with ADMIN but no USER bindings, and no custom default group, is not treated as complete."""
        mapping = self._opted_in_tenant("backfill-missing-user-org")
        backfill_default_role_bindings()
        RoleBindingService(mapping.tenant).delete_user_default_bindings()

        result = backfill_default_role_bindings()

        self.assertEqual(result.tenants_backfilled, 1)
        self.assertEqual(result.already_complete, 0)
        self.assertEqual(self._count_default_bindings(mapping, DefaultAccessType.USER), len(CONCRETE_SCOPES))

    def test_run_aborts_after_consecutive_unsuccessful_tenants(self):
        """A systemic failure stops the run instead of walking every remaining tenant."""
        for i in range(ABORT_THRESHOLD + 2):
            self._opted_in_tenant(f"backfill-abort-org-{i}")

        with patch(CREATE_ALL_DEFAULT_BINDINGS, side_effect=DefaultGroupNotAvailableError()):
            result = backfill_default_role_bindings()

        self.assertTrue(result.aborted)
        self.assertEqual(result.tenants_processed, ABORT_THRESHOLD)
        self.assertEqual(result.incomplete, ABORT_THRESHOLD)

    def test_batching_processes_every_tenant(self):
        """A batch size smaller than the tenant count still processes all of them."""
        mappings = [self._opted_in_tenant(f"backfill-batch-org-{i}") for i in range(3)]

        result = backfill_default_role_bindings(batch_size=2)

        self.assertEqual(result.tenants_processed, 3)
        self.assertEqual(result.tenants_backfilled, 3)
        for mapping in mappings:
            self.assertEqual(self._count_default_bindings(mapping, DefaultAccessType.ADMIN), len(CONCRETE_SCOPES))


class BackfillDefaultRoleBindingsCommandTests(IdentityRequest):
    """Tests for the backfill_default_role_bindings management command."""

    BACKFILL = "management.management.commands.backfill_default_role_bindings.backfill_default_role_bindings"

    def test_requires_all_or_org_ids(self):
        with self.assertRaises(CommandError):
            call_command("backfill_default_role_bindings")

    def test_all_backfills_every_tenant(self):
        with patch(self.BACKFILL, return_value=BackfillResult()) as mock_backfill:
            call_command("backfill_default_role_bindings", "--all")

        mock_backfill.assert_called_once_with(org_ids=[], batch_size=500)

    def test_org_ids_are_parsed_and_stripped(self):
        with patch(self.BACKFILL, return_value=BackfillResult()) as mock_backfill:
            call_command("backfill_default_role_bindings", "--org-ids= 123 , ,456 ")

        mock_backfill.assert_called_once_with(org_ids=["123", "456"], batch_size=500)

    def test_blank_org_ids_with_all_processes_everything(self):
        with patch(self.BACKFILL, return_value=BackfillResult()) as mock_backfill:
            call_command("backfill_default_role_bindings", "--all", "--org-ids=")

        mock_backfill.assert_called_once_with(org_ids=[], batch_size=500)

    def test_batch_size_is_forwarded(self):
        with patch(self.BACKFILL, return_value=BackfillResult()) as mock_backfill:
            call_command("backfill_default_role_bindings", "--all", "--batch-size=10")

        mock_backfill.assert_called_once_with(org_ids=[], batch_size=10)

    def test_non_positive_batch_size_is_rejected(self):
        with patch(self.BACKFILL) as mock_backfill:
            with self.assertRaises(CommandError):
                call_command("backfill_default_role_bindings", "--all", "--batch-size=0")

        mock_backfill.assert_not_called()

    def test_errors_fail_the_command(self):
        with patch(self.BACKFILL, return_value=BackfillResult(failed_orgs=["boom-org"])):
            with self.assertRaises(CommandError) as ctx:
                call_command("backfill_default_role_bindings", "--all")

        self.assertIn("boom-org", str(ctx.exception))

    def test_incomplete_tenants_fail_the_command(self):
        with patch(self.BACKFILL, return_value=BackfillResult(incomplete_orgs=["unseeded-org"])):
            with self.assertRaises(CommandError) as ctx:
                call_command("backfill_default_role_bindings", "--all")

        self.assertIn("unseeded-org", str(ctx.exception))


class BackfillDefaultRoleBindingsAPITests(IdentityRequest):
    """Tests for the backfill_default_role_bindings internal API endpoint."""

    BACKFILL = "internal.migrations.backfill_role_bindings.backfill_default_role_bindings"
    URL = "/_private/api/utils/backfill_default_role_bindings/"

    def setUp(self):
        """Set up internal request context for auth."""
        super().setUp()
        self.internal_request_context = self._create_request_context(
            self.customer_data, self.user_data, is_internal=True
        )
        self.request = self.internal_request_context["request"]

    def test_post_backfills_all_tenants(self):
        """POST without org_ids processes all opted-in tenants."""
        with patch(self.BACKFILL, return_value=BackfillResult(tenants_processed=5, tenants_backfilled=3)) as mock:
            response = self.client.post(self.URL, **self.request.META)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["tenants_processed"], 5)
        self.assertEqual(data["tenants_backfilled"], 3)
        mock.assert_called_once_with(org_ids=None, batch_size=500)

    def test_post_with_org_ids(self):
        """POST with org_ids restricts the backfill."""
        with patch(self.BACKFILL, return_value=BackfillResult()) as mock:
            response = self.client.post(f"{self.URL}?org_ids=org-1,org-2", **self.request.META)

        self.assertEqual(response.status_code, 200)
        mock.assert_called_once_with(org_ids=["org-1", "org-2"], batch_size=500)

    def test_post_with_batch_size(self):
        """POST with batch_size forwards it to the backfill function."""
        with patch(self.BACKFILL, return_value=BackfillResult()) as mock:
            response = self.client.post(f"{self.URL}?batch_size=10", **self.request.META)

        self.assertEqual(response.status_code, 200)
        mock.assert_called_once_with(org_ids=None, batch_size=10)

    def test_post_with_org_ids_and_batch_size(self):
        """POST with both org_ids and batch_size."""
        with patch(self.BACKFILL, return_value=BackfillResult()) as mock:
            response = self.client.post(f"{self.URL}?org_ids=abc&batch_size=50", **self.request.META)

        self.assertEqual(response.status_code, 200)
        mock.assert_called_once_with(org_ids=["abc"], batch_size=50)

    def test_empty_org_ids_returns_400(self):
        """Empty org_ids param is rejected to prevent unfiltered backfill."""
        with patch(self.BACKFILL) as mock:
            response = self.client.post(f"{self.URL}?org_ids=", **self.request.META)
        self.assertEqual(response.status_code, 400)
        self.assertIn("org_ids must contain at least one org ID", response.json()["detail"])
        mock.assert_not_called()

    def test_whitespace_only_org_ids_returns_400(self):
        """org_ids with only commas/whitespace is rejected."""
        with patch(self.BACKFILL) as mock:
            response = self.client.post(f"{self.URL}?org_ids=%20,%20,", **self.request.META)
        self.assertEqual(response.status_code, 400)
        mock.assert_not_called()

    def test_invalid_batch_size_returns_400(self):
        """Non-integer batch_size is rejected without starting the backfill."""
        with patch(self.BACKFILL) as mock:
            response = self.client.post(f"{self.URL}?batch_size=abc", **self.request.META)
        self.assertEqual(response.status_code, 400)
        mock.assert_not_called()

    def test_zero_batch_size_returns_400(self):
        """Zero batch_size is rejected without starting the backfill."""
        with patch(self.BACKFILL) as mock:
            response = self.client.post(f"{self.URL}?batch_size=0", **self.request.META)
        self.assertEqual(response.status_code, 400)
        mock.assert_not_called()

    def test_negative_batch_size_returns_400(self):
        """Negative batch_size is rejected without starting the backfill."""
        with patch(self.BACKFILL) as mock:
            response = self.client.post(f"{self.URL}?batch_size=-1", **self.request.META)
        self.assertEqual(response.status_code, 400)
        mock.assert_not_called()

    def test_get_not_allowed(self):
        """GET is rejected with 405 without starting the backfill."""
        with patch(self.BACKFILL) as mock:
            response = self.client.get(self.URL, **self.request.META)
        self.assertEqual(response.status_code, 405)
        mock.assert_not_called()

    def test_response_includes_all_result_fields(self):
        """Response includes all BackfillResult fields (partial failure → 422)."""
        result = BackfillResult(
            tenants_processed=10,
            already_complete=5,
            tenants_backfilled=3,
            incomplete_orgs=["inc-org"],
            failed_orgs=["fail-org"],
            aborted=False,
        )
        with patch(self.BACKFILL, return_value=result):
            response = self.client.post(self.URL, **self.request.META)

        self.assertEqual(response.status_code, 422)
        data = response.json()
        self.assertEqual(data["tenants_processed"], 10)
        self.assertEqual(data["already_complete"], 5)
        self.assertEqual(data["tenants_backfilled"], 3)
        self.assertEqual(data["incomplete"], 1)
        self.assertEqual(data["incomplete_orgs"], ["inc-org"])
        self.assertEqual(data["errors"], 1)
        self.assertEqual(data["failed_orgs"], ["fail-org"])
        self.assertFalse(data["aborted"])

    def test_errors_returns_422(self):
        """A result with failed tenants returns 422."""
        result = BackfillResult(tenants_processed=5, tenants_backfilled=3, failed_orgs=["org-x"])
        with patch(self.BACKFILL, return_value=result):
            response = self.client.post(self.URL, **self.request.META)

        self.assertEqual(response.status_code, 422)
        data = response.json()
        self.assertEqual(data["errors"], 1)
        self.assertEqual(data["failed_orgs"], ["org-x"])

    def test_incomplete_returns_422(self):
        """A result with incomplete tenants returns 422."""
        result = BackfillResult(tenants_processed=5, tenants_backfilled=3, incomplete_orgs=["org-y"])
        with patch(self.BACKFILL, return_value=result):
            response = self.client.post(self.URL, **self.request.META)

        self.assertEqual(response.status_code, 422)
        data = response.json()
        self.assertEqual(data["incomplete"], 1)

    def test_aborted_returns_500(self):
        """An aborted backfill returns 500."""
        result = BackfillResult(tenants_processed=5, aborted=True, failed_orgs=["a", "b", "c"])
        with patch(self.BACKFILL, return_value=result):
            response = self.client.post(self.URL, **self.request.META)

        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.json()["aborted"])

    def test_exception_returns_500(self):
        """An unexpected exception returns a 500 with a fixed error message."""
        with patch(self.BACKFILL, side_effect=RuntimeError("db gone")):
            response = self.client.post(self.URL, **self.request.META)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "Error running backfill")
        self.assertNotIn("db gone", response.json()["detail"])
