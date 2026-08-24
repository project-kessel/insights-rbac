#
# Copyright 2026 Red Hat, Inc.
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
"""Tests for the ensure_user management command."""

from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test.utils import override_settings

from api.models import Tenant
from management.models import Access, Group, Permission, Policy, Principal, Role
from tests.identity_request import IdentityRequest


@override_settings(V2_BOOTSTRAP_TENANT=True)
class TestEnsureUser(IdentityRequest):
    """Test ensure_user command."""

    def setUp(self):
        super().setUp()
        self.public_tenant = Tenant.objects.get(tenant_name="public")
        Tenant.objects.exclude(tenant_name="public").delete()

    def _create_role(self, name, application, admin_default=False):
        permission, _ = Permission.objects.get_or_create(
            application=application,
            resource_type="*",
            verb="*",
            defaults={"permission": f"{application}:*:*", "tenant": self.public_tenant},
        )
        role, _ = Role.objects.get_or_create(
            name=name,
            tenant=self.public_tenant,
            defaults={
                "description": name,
                "system": True,
                "admin_default": admin_default,
                "version": 2,
            },
        )
        role.admin_default = admin_default
        role.save()
        Access.objects.get_or_create(role=role, permission=permission, defaults={"tenant": self.public_tenant})
        return role

    def _invoke(self, *args, **kwargs):
        call_command("ensure_user", *args, **kwargs)

    def test_missing_required_flags(self):
        """Required flags must be provided."""
        with self.assertRaises(CommandError):
            self._invoke("--username=alice")

    def test_missing_public_tenant(self):
        """Fails with a clear error when migrations/seeds have not run."""
        Tenant.objects.filter(tenant_name="public").delete()
        with self.assertRaisesMessage(CommandError, "Public tenant not found"):
            self._invoke(
                "--username=alice",
                "--org-id=org1",
                "--account-number=123",
            )

    def test_admin_requires_application(self):
        """--admin without --application uses all admin_default roles."""
        self._create_role("Cost Administrator", "cost-management", admin_default=True)
        self._create_role("Sources administrator", "sources", admin_default=True)

        with patch("management.management.commands.ensure_user.call_command"):
            self._invoke(
                "--username=alice",
                "--org-id=org1",
                "--account-number=123",
                "--admin",
            )

        tenant = Tenant.objects.get(org_id="org1")
        policy = Policy.objects.get(tenant=tenant)
        role_names = set(policy.roles.values_list("name", flat=True))
        self.assertEqual(role_names, {"Cost Administrator", "Sources administrator"})

    def test_admin_fails_when_no_matching_roles(self):
        """--admin fails when seeds have not created admin_default roles."""
        with self.assertRaisesMessage(CommandError, "No admin_default roles found"):
            self._invoke(
                "--username=alice",
                "--org-id=org1",
                "--account-number=123",
                "--application=cost-management",
                "--admin",
            )

    @patch("management.management.commands.ensure_user.call_command")
    def test_creates_tenant_and_principal_without_admin(self, mock_bootstrap):
        """Without --admin, only tenant and principal are created."""
        self._invoke(
            "--username=alice",
            "--org-id=org1",
            "--account-number=123",
        )

        tenant = Tenant.objects.get(org_id="org1")
        self.assertEqual(tenant.tenant_name, "acct123")
        self.assertTrue(tenant.ready)
        principal = Principal.objects.get(username="alice", tenant=tenant)
        self.assertEqual(principal.type, Principal.Types.USER)
        self.assertFalse(Group.objects.filter(tenant=tenant, admin_default=True).exists())
        mock_bootstrap.assert_called_once_with("bootstrap_tenants", "--org-id", "org1", "--force", verbosity=1)

    @patch("management.management.commands.ensure_user.call_command")
    def test_admin_grants_filtered_roles(self, mock_bootstrap):
        """--admin adds principal to org admin-default group with filtered roles."""
        cost_role = self._create_role("Cost Administrator", "cost-management", admin_default=True)
        self._create_role("Sources administrator", "sources", admin_default=True)
        self._create_role("Other Admin", "inventory", admin_default=True)

        self._invoke(
            "--username=alice",
            "--org-id=org1",
            "--account-number=123",
            "--application=cost-management",
            "--application=sources",
            "--admin",
        )

        tenant = Tenant.objects.get(org_id="org1")
        principal = Principal.objects.get(username="alice", tenant=tenant)
        group = Group.objects.get(tenant=tenant, admin_default=True)
        self.assertIn(principal, group.principals.all())

        policy = Policy.objects.get(group=group, tenant=tenant)
        role_names = set(policy.roles.values_list("name", flat=True))
        self.assertEqual(role_names, {"Cost Administrator", "Sources administrator"})
        self.assertNotIn("Other Admin", role_names)
        self.assertIn(cost_role, policy.roles.all())
        mock_bootstrap.assert_called_once()

    @patch("management.management.commands.ensure_user.call_command")
    def test_idempotent_rerun(self, mock_bootstrap):
        """Re-running ensure_user does not duplicate tenant, principal, or membership."""
        self._create_role("Cost Administrator", "cost-management", admin_default=True)

        args = (
            "--username=alice",
            "--org-id=org1",
            "--account-number=123",
            "--application=cost-management",
            "--admin",
        )
        self._invoke(*args)
        self._invoke(*args)

        self.assertEqual(Tenant.objects.filter(org_id="org1").count(), 1)
        tenant = Tenant.objects.get(org_id="org1")
        self.assertEqual(Principal.objects.filter(username="alice", tenant=tenant).count(), 1)
        self.assertEqual(Group.objects.filter(tenant=tenant, admin_default=True).count(), 1)
        self.assertEqual(mock_bootstrap.call_count, 2)

    @patch("management.management.commands.ensure_user.call_command")
    def test_custom_admin_group_and_policy_names(self, mock_bootstrap):
        """--admin-group-name and --admin-policy-name create named group and policy."""
        self._create_role("Cost Administrator", "cost-management", admin_default=True)

        self._invoke(
            "--username=alice",
            "--org-id=org1",
            "--account-number=123",
            "--application=cost-management",
            "--admin",
            "--admin-group-name=Cost Admin Default",
            "--admin-group-description=Admin default: grants admin_default roles to bootstrap admin user",
            "--admin-policy-name=Cost Admin Default Policy",
        )

        tenant = Tenant.objects.get(org_id="org1")
        group = Group.objects.get(tenant=tenant, name="Cost Admin Default")
        self.assertTrue(group.admin_default)
        policy = Policy.objects.get(tenant=tenant, group=group, name="Cost Admin Default Policy")
        self.assertEqual(policy.roles.count(), 1)
        mock_bootstrap.assert_called_once()

    @patch("management.management.commands.ensure_user.call_command")
    def test_reuses_existing_cost_admin_default_group(self, mock_bootstrap):
        """Existing per-org Cost Admin Default group is reused (not renamed)."""
        self._create_role("Cost Administrator", "cost-management", admin_default=True)
        tenant = Tenant.objects.create(tenant_name="acct123", org_id="org1", ready=True)
        existing_group = Group.objects.create(
            name="Cost Admin Default",
            tenant=tenant,
            admin_default=True,
            system=True,
        )

        self._invoke(
            "--username=alice",
            "--org-id=org1",
            "--account-number=123",
            "--application=cost-management",
            "--admin",
            "--admin-group-name=Cost Admin Default",
            "--admin-policy-name=Cost Admin Default Policy",
        )

        group = Group.objects.get(tenant=tenant, admin_default=True)
        self.assertEqual(group.pk, existing_group.pk)
        self.assertEqual(group.name, "Cost Admin Default")
        principal = Principal.objects.get(username="alice", tenant=tenant)
        self.assertIn(principal, group.principals.all())
        mock_bootstrap.assert_called_once()

    @patch("management.management.commands.ensure_user.AccessCache")
    @patch("management.management.commands.ensure_user.call_command")
    def test_invalidates_tenant_policy_cache_after_commit(self, mock_bootstrap, mock_cache_cls):
        """After commit, only this tenant's policy cache is purged."""
        cache = mock_cache_cls.return_value

        with self.captureOnCommitCallbacks(execute=True):
            self._invoke(
                "--username=alice",
                "--org-id=org1",
                "--account-number=123",
            )

        mock_cache_cls.assert_called_once_with("org1")
        cache.delete_all_policies_for_tenant.assert_called_once_with()
        mock_bootstrap.assert_called_once()

    @patch("management.management.commands.ensure_user.AccessCache")
    @patch("management.management.commands.ensure_user.call_command")
    def test_cache_invalidation_failure_does_not_skip_bootstrap(self, mock_bootstrap, mock_cache_cls):
        """Redis failures after commit must not prevent bootstrap_tenants."""
        mock_cache_cls.return_value.delete_all_policies_for_tenant.side_effect = Exception("redis down")

        with self.captureOnCommitCallbacks(execute=True):
            self._invoke(
                "--username=alice",
                "--org-id=org1",
                "--account-number=123",
            )

        tenant = Tenant.objects.get(org_id="org1")
        self.assertTrue(Principal.objects.filter(username="alice", tenant=tenant).exists())
        mock_bootstrap.assert_called_once_with("bootstrap_tenants", "--org-id", "org1", "--force", verbosity=1)

    @patch("management.management.commands.ensure_user.call_command")
    def test_bootstrap_failure_raises_command_error(self, mock_bootstrap):
        """bootstrap_tenants failure after commit is reported without rolling back the user."""
        mock_bootstrap.side_effect = RuntimeError("kessel unavailable")

        with self.assertRaises(CommandError):
            self._invoke(
                "--username=alice",
                "--org-id=org1",
                "--account-number=123",
            )

        tenant = Tenant.objects.get(org_id="org1")
        self.assertTrue(Principal.objects.filter(username="alice", tenant=tenant).exists())
        mock_bootstrap.assert_called_once_with("bootstrap_tenants", "--org-id", "org1", "--force", verbosity=1)

    def test_skip_bootstrap_does_not_call_bootstrap_tenants(self):
        """--skip-bootstrap creates the user without invoking bootstrap_tenants."""
        with patch("management.management.commands.ensure_user.call_command") as mock_bootstrap:
            self._invoke(
                "--username=alice",
                "--org-id=org1",
                "--account-number=123",
                "--skip-bootstrap",
            )

        tenant = Tenant.objects.get(org_id="org1")
        self.assertTrue(Principal.objects.filter(username="alice", tenant=tenant).exists())
        mock_bootstrap.assert_not_called()
