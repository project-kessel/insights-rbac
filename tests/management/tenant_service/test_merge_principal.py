"""Tests for merging duplicate principals during user_id assignment."""

from unittest.mock import patch

from django.db import IntegrityError
from django.test import override_settings

from api.models import Tenant, User
from management.group.model import Group
from management.principal.model import Principal
from management.role_binding.model import RoleBinding, RoleBindingPrincipal
from management.role.v2_model import CustomRoleV2
from management.tenant_mapping.model import TenantMapping
from management.tenant_service.tenant_service import (
    _ensure_principal_with_user_id_in_tenant,
    merge_obsolete_principal_into_survivor,
    rewrite_principal_user_id,
)
from migration_tool.in_memory_tuples import (
    InMemoryRelationReplicator,
    InMemoryTuples,
    all_of,
    relation,
    resource,
    subject,
)
from tests.identity_request import IdentityRequest
from tests.v2_util import bootstrap_tenant_for_v2_test


class _ReplicationTracker:
    """Records replication add/remove lists for assertions."""

    def __init__(self):
        self.tuples_added = []
        self.tuples_removed = []

    def replicate(self, event):
        self.tuples_added.extend(event.add)
        self.tuples_removed.extend(event.remove)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class MergePrincipalTests(IdentityRequest):
    """Tests for obsolete-into-survivor principal merge."""

    def setUp(self):
        super().setUp()
        bootstrap_tenant_for_v2_test(self.tenant)

    def test_merge_keeps_survivor_group_membership_and_assigns_user_id(self):
        """Survivor (no user_id) keeps its groups and receives user_id; obsolete is deleted."""
        obsolete = Principal.objects.create(username="jaross@redhat.com", tenant=self.tenant, user_id="54181241")
        survivor = Principal.objects.create(username="jdross@redhat.com", tenant=self.tenant)
        group = Group.objects.create(name="engineering", tenant=self.tenant)
        group.principals.add(survivor)

        tracker = _ReplicationTracker()
        result = merge_obsolete_principal_into_survivor(survivor, obsolete, user_id="54181241", replicator=tracker)
        self.assertIsNone(result)

        self.assertFalse(Principal.objects.filter(pk=obsolete.pk).exists())
        survivor.refresh_from_db()
        self.assertEqual(survivor.user_id, "54181241")
        self.assertEqual(list(group.principals.all()), [survivor])

    def test_merge_transfers_obsolete_groups_to_survivor(self):
        """Groups only the obsolete principal belonged to are added to the survivor."""
        obsolete = Principal.objects.create(username="jaross@redhat.com", tenant=self.tenant, user_id="54181241")
        survivor = Principal.objects.create(username="jdross@redhat.com", tenant=self.tenant)
        survivor_group = Group.objects.create(name="engineering", tenant=self.tenant)
        obsolete_group = Group.objects.create(name="legacy", tenant=self.tenant)
        survivor_group.principals.add(survivor)
        obsolete_group.principals.add(obsolete)

        tracker = _ReplicationTracker()
        result = merge_obsolete_principal_into_survivor(survivor, obsolete, user_id="54181241", replicator=tracker)
        self.assertIsNone(result)

        survivor.refresh_from_db()
        self.assertEqual(survivor.user_id, "54181241")
        self.assertCountEqual(survivor.group.values_list("name", flat=True), ["engineering", "legacy"])

    def test_merge_replicates_group_member_tuple_for_survivor(self):
        """V2 tenants replicate group membership tuples for the survivor after user_id assignment."""
        obsolete = Principal.objects.create(username="jaross@redhat.com", tenant=self.tenant, user_id="54181241")
        survivor = Principal.objects.create(username="jdross@redhat.com", tenant=self.tenant)
        group = Group.objects.create(name="engineering", tenant=self.tenant)
        group.principals.add(survivor)

        tuples = InMemoryTuples()
        replicator = InMemoryRelationReplicator(tuples)
        result = merge_obsolete_principal_into_survivor(survivor, obsolete, user_id="54181241", replicator=replicator)
        self.assertIsNone(result)

        self.assertEqual(
            tuples.count_tuples(
                all_of(
                    resource("rbac", "group", str(group.uuid)),
                    relation("member"),
                    subject("rbac", "principal", "redhat/54181241"),
                )
            ),
            1,
        )

    def test_merge_removes_obsolete_group_member_tuple(self):
        """SpiceDB tuples for the obsolete principal are removed from its former groups."""
        obsolete = Principal.objects.create(username="jaross@redhat.com", tenant=self.tenant, user_id="54181241")
        survivor = Principal.objects.create(username="jdross@redhat.com", tenant=self.tenant)
        obsolete_group = Group.objects.create(name="legacy", tenant=self.tenant)
        obsolete_group.principals.add(obsolete)

        tracker = _ReplicationTracker()
        result = merge_obsolete_principal_into_survivor(survivor, obsolete, user_id="54181241", replicator=tracker)
        self.assertIsNone(result)

        expected_removed = Group.relationship_to_user_id_for_group(str(obsolete_group.uuid), "54181241")
        survivor.refresh_from_db()
        expected_added = obsolete_group.relationship_to_principal(survivor)
        self.assertCountEqual(tracker.tuples_removed, [expected_removed])
        self.assertCountEqual(tracker.tuples_added, [expected_added])

    def test_merge_transfers_role_binding_entries(self):
        """Direct role-binding entries on the obsolete principal move to the survivor."""
        obsolete = Principal.objects.create(username="jaross@redhat.com", tenant=self.tenant, user_id="54181241")
        survivor = Principal.objects.create(username="jdross@redhat.com", tenant=self.tenant)
        role = CustomRoleV2.objects.create(name="merge-test-role", tenant=self.tenant)
        binding = RoleBinding.objects.create(
            role=role,
            resource_type="workspace",
            resource_id="00000000-0000-0000-0000-000000000001",
            tenant=self.tenant,
        )
        RoleBindingPrincipal.objects.create(binding=binding, principal=obsolete, source="direct")

        obsolete_id = obsolete.pk
        tracker = _ReplicationTracker()
        result = merge_obsolete_principal_into_survivor(survivor, obsolete, user_id="54181241", replicator=tracker)
        self.assertIsNone(result)

        survivor.refresh_from_db()
        self.assertEqual(survivor.user_id, "54181241")
        self.assertFalse(Principal.objects.filter(pk=obsolete_id).exists())
        self.assertFalse(RoleBindingPrincipal.objects.filter(principal_id=obsolete_id).exists())
        self.assertTrue(
            RoleBindingPrincipal.objects.filter(binding=binding, principal=survivor, source="direct").exists()
        )

    def test_merge_raises_on_cross_tenant(self):
        """Cross-tenant merge raises RuntimeError; both principals are left unchanged."""
        other_tenant = Tenant.objects.create(
            tenant_name="other-tenant",
            account_id="99999999",
            org_id="99999999",
            ready=True,
        )
        obsolete = Principal.objects.create(username="jaross@redhat.com", tenant=other_tenant, user_id="54181241")
        survivor = Principal.objects.create(username="jdross@redhat.com", tenant=self.tenant)
        other_group = Group.objects.create(name="other-org-group", tenant=other_tenant)
        other_group.principals.add(obsolete)

        tracker = _ReplicationTracker()
        with self.assertRaises(RuntimeError):
            merge_obsolete_principal_into_survivor(survivor, obsolete, user_id="54181241", replicator=tracker)

        self.assertTrue(Principal.objects.filter(pk=obsolete.pk).exists())
        survivor.refresh_from_db()
        self.assertIsNone(survivor.user_id)
        self.assertEqual(list(other_group.principals.all()), [obsolete])

    def test_ensure_principal_merges_on_user_id_conflict(self):
        """Assigning a taken user_id merges obsolete into the current username principal."""
        obsolete = Principal.objects.create(username="jaross@redhat.com", tenant=self.tenant, user_id="54181241")
        survivor = Principal.objects.create(username="jdross@redhat.com", tenant=self.tenant)
        group = Group.objects.create(name="engineering", tenant=self.tenant)
        group.principals.add(survivor)

        user = User()
        user.username = "jdross@redhat.com"
        user.user_id = "54181241"
        user.org_id = self.tenant.org_id

        tracker = _ReplicationTracker()
        result = _ensure_principal_with_user_id_in_tenant(user, self.tenant, replicator=tracker)
        self.assertIsNone(result)

        self.assertFalse(Principal.objects.filter(pk=obsolete.pk).exists())
        survivor.refresh_from_db()
        self.assertEqual(survivor.user_id, "54181241")
        self.assertEqual(list(group.principals.all()), [survivor])

    def test_ensure_principal_rewrites_mismatched_user_id_and_merges_owner(self):
        """Mismatch with another owner of the BOP user_id rewrites and merges the owner."""
        obsolete = Principal.objects.create(username="jaross@redhat.com", tenant=self.tenant, user_id="54181241")
        principal = Principal.objects.create(username="jdross@redhat.com", tenant=self.tenant, user_id="99999999")
        obsolete_group = Group.objects.create(name="legacy", tenant=self.tenant)
        principal_group = Group.objects.create(name="engineering", tenant=self.tenant)
        shared_group = Group.objects.create(name="shared", tenant=self.tenant)
        obsolete_group.principals.add(obsolete)
        principal_group.principals.add(principal)
        shared_group.principals.add(obsolete, principal)

        user = User()
        user.username = "jdross@redhat.com"
        user.user_id = "54181241"
        user.org_id = self.tenant.org_id

        tracker = _ReplicationTracker()
        result = _ensure_principal_with_user_id_in_tenant(user, self.tenant, replicator=tracker)
        self.assertIsNone(result)

        principal.refresh_from_db()
        self.assertEqual(principal.user_id, "54181241")
        self.assertFalse(Principal.objects.filter(pk=obsolete.pk).exists())
        self.assertCountEqual(principal.group.values_list("name", flat=True), ["engineering", "legacy", "shared"])

        expected_removed = [
            Group.relationship_to_user_id_for_group(str(principal_group.uuid), "99999999"),
            Group.relationship_to_user_id_for_group(str(shared_group.uuid), "99999999"),
            Group.relationship_to_user_id_for_group(str(obsolete_group.uuid), "54181241"),
            Group.relationship_to_user_id_for_group(str(shared_group.uuid), "54181241"),
        ]
        expected_added = [
            Group.relationship_to_user_id_for_group(str(principal_group.uuid), "54181241"),
            Group.relationship_to_user_id_for_group(str(shared_group.uuid), "54181241"),
            Group.relationship_to_user_id_for_group(str(obsolete_group.uuid), "54181241"),
        ]
        # Default-group tuples (via TenantMapping, not M2M)
        mapping = TenantMapping.objects.get(tenant=self.tenant)
        for default_uuid in (mapping.default_group_uuid, mapping.default_admin_group_uuid):
            expected_removed.append(Group.relationship_to_user_id_for_group(str(default_uuid), "99999999"))
            expected_added.append(Group.relationship_to_user_id_for_group(str(default_uuid), "54181241"))
        self.assertCountEqual(tracker.tuples_removed, expected_removed)
        self.assertCountEqual(tracker.tuples_added, expected_added)

    def test_ensure_principal_upsert_integrity_error_creates_survivor_when_missing(self):
        """upsert IntegrityError path creates survivor principal when insert fails on user_id uniqueness."""
        obsolete = Principal.objects.create(username="jaross@redhat.com", tenant=self.tenant, user_id="54181241")
        user = User()
        user.username = "jdross@redhat.com"
        user.user_id = "54181241"
        user.org_id = self.tenant.org_id

        original_get_or_create = Principal.objects.get_or_create
        call_count = {"n": 0}

        def flaky_get_or_create(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise IntegrityError(
                    'duplicate key value violates unique constraint "management_principal_user_id_key"'
                )
            return original_get_or_create(*args, **kwargs)

        tracker = _ReplicationTracker()
        with patch.object(Principal.objects, "get_or_create", side_effect=flaky_get_or_create):
            result = _ensure_principal_with_user_id_in_tenant(user, self.tenant, upsert=True, replicator=tracker)
        self.assertIsNone(result)

        self.assertTrue(Principal.objects.filter(username="jdross@redhat.com", tenant=self.tenant).exists())
        self.assertFalse(Principal.objects.filter(pk=obsolete.pk).exists())
        survivor = Principal.objects.get(username="jdross@redhat.com", tenant=self.tenant)
        self.assertEqual(survivor.user_id, "54181241")

    def test_resolve_user_id_conflict_refreshes_stale_in_memory_user_id(self):
        """After a failed save, in-memory user_id may already be the target; refresh before rewrite."""
        from management.tenant_service.tenant_service import _resolve_user_id_conflict

        obsolete = Principal.objects.create(username="jaross@redhat.com", tenant=self.tenant, user_id="54181241")
        principal = Principal.objects.create(username="jdross@redhat.com", tenant=self.tenant, user_id="99999999")
        group = Group.objects.create(name="engineering", tenant=self.tenant)
        group.principals.add(principal)

        # Simulate assign path: caller set principal.user_id to the target before save() failed.
        principal.user_id = "54181241"
        tracker = _ReplicationTracker()
        _resolve_user_id_conflict(principal, "54181241", tracker)

        principal.refresh_from_db()
        self.assertEqual(principal.user_id, "54181241")
        self.assertFalse(Principal.objects.filter(pk=obsolete.pk).exists())

        expected_removed = [Group.relationship_to_user_id_for_group(str(group.uuid), "99999999")]
        expected_added = [Group.relationship_to_user_id_for_group(str(group.uuid), "54181241")]
        # Default-group tuples (via TenantMapping, not M2M)
        mapping = TenantMapping.objects.get(tenant=self.tenant)
        for default_uuid in (mapping.default_group_uuid, mapping.default_admin_group_uuid):
            expected_removed.append(Group.relationship_to_user_id_for_group(str(default_uuid), "99999999"))
            expected_added.append(Group.relationship_to_user_id_for_group(str(default_uuid), "54181241"))
        self.assertCountEqual(tracker.tuples_removed, expected_removed)
        self.assertCountEqual(tracker.tuples_added, expected_added)

    def test_ensure_principal_noop_when_principal_already_has_user_id(self):
        """No merge when the matched principal already has the expected user_id."""
        principal = Principal.objects.create(username="jaross@redhat.com", tenant=self.tenant, user_id="54181241")
        user = User()
        user.username = "jaross@redhat.com"
        user.user_id = "54181241"
        user.org_id = self.tenant.org_id

        tracker = _ReplicationTracker()
        result = _ensure_principal_with_user_id_in_tenant(user, self.tenant, replicator=tracker)
        self.assertIsNone(result)

        self.assertTrue(Principal.objects.filter(pk=principal.pk).exists())
        self.assertEqual(Principal.objects.count(), 1)

    def test_ensure_principal_noop_when_bop_user_id_is_int(self):
        """BOP may return user_id as int; it should match the string stored on Principal."""
        principal = Principal.objects.create(username="jrobbins@redhat.com", tenant=self.tenant, user_id="7919876")
        user = User()
        user.username = "jrobbins@redhat.com"
        user.user_id = 7919876
        user.org_id = self.tenant.org_id

        tracker = _ReplicationTracker()
        result = _ensure_principal_with_user_id_in_tenant(user, self.tenant, replicator=tracker)
        self.assertIsNone(result)

        principal.refresh_from_db()
        self.assertEqual(principal.user_id, "7919876")
        self.assertEqual(Principal.objects.count(), 1)


@override_settings(ATOMIC_RETRY_DISABLED=True)
class RewritePrincipalUserIdTests(IdentityRequest):
    """Tests for in-place principal user_id rewrite with SpiceDB tuple replication."""

    def setUp(self):
        super().setUp()
        bootstrap_tenant_for_v2_test(self.tenant)

    def test_rewrite_noop_when_user_id_already_matches(self):
        """No DB or replication work when the principal already has the target user_id."""
        principal = Principal.objects.create(
            username="teppo.haapala@apotti.fi", tenant=self.tenant, user_id="51856862"
        )
        group = Group.objects.create(name="admins", tenant=self.tenant)
        group.principals.add(principal)

        tracker = _ReplicationTracker()
        self.assertIsNone(rewrite_principal_user_id(principal, "51856862", tracker))

        principal.refresh_from_db()
        self.assertEqual(principal.user_id, "51856862")
        self.assertEqual(tracker.tuples_added, [])
        self.assertEqual(tracker.tuples_removed, [])

    def test_rewrite_updates_user_id_and_replicates_group_member_tuples(self):
        """In-place rewrite removes old group member tuples and adds new ones."""
        principal = Principal.objects.create(
            username="teppo.haapala@apotti.fi", tenant=self.tenant, user_id="16777216"
        )
        group = Group.objects.create(name="admins", tenant=self.tenant)
        group.principals.add(principal)

        tracker = _ReplicationTracker()
        self.assertIsNone(rewrite_principal_user_id(principal, "51856862", tracker))

        principal.refresh_from_db()
        self.assertEqual(principal.user_id, "51856862")
        self.assertEqual(list(group.principals.all()), [principal])

        expected_removed = [Group.relationship_to_user_id_for_group(str(group.uuid), "16777216")]
        expected_added = [Group.relationship_to_user_id_for_group(str(group.uuid), "51856862")]
        # Default-group tuples (via TenantMapping, not M2M)
        mapping = TenantMapping.objects.get(tenant=self.tenant)
        for default_uuid in (mapping.default_group_uuid, mapping.default_admin_group_uuid):
            expected_removed.append(Group.relationship_to_user_id_for_group(str(default_uuid), "16777216"))
            expected_added.append(Group.relationship_to_user_id_for_group(str(default_uuid), "51856862"))
        self.assertCountEqual(tracker.tuples_removed, expected_removed)
        self.assertCountEqual(tracker.tuples_added, expected_added)

    def test_rewrite_replicates_direct_role_binding_subject_tuples(self):
        """Direct role-binding subject tuples are rewritten from old user_id to new."""
        principal = Principal.objects.create(
            username="teppo.haapala@apotti.fi", tenant=self.tenant, user_id="16777216"
        )
        role = CustomRoleV2.objects.create(name="rewrite-test-role", tenant=self.tenant)
        binding = RoleBinding.objects.create(
            role=role,
            resource_type="workspace",
            resource_id="00000000-0000-0000-0000-000000000001",
            tenant=self.tenant,
        )
        RoleBindingPrincipal.objects.create(binding=binding, principal=principal, source="direct")

        tracker = _ReplicationTracker()
        self.assertIsNone(rewrite_principal_user_id(principal, "51856862", tracker))

        principal.refresh_from_db()
        self.assertEqual(principal.user_id, "51856862")

        expected_removed = [
            binding._user_subject_tuple(Principal(username="tmp", tenant=self.tenant, user_id="16777216"))
        ]
        # Rebuild expected with the same binding uuid after rewrite (principal now has new id).
        expected_added = [binding._user_subject_tuple(principal)]
        # Default-group tuples (via TenantMapping, not M2M)
        mapping = TenantMapping.objects.get(tenant=self.tenant)
        for default_uuid in (mapping.default_group_uuid, mapping.default_admin_group_uuid):
            expected_removed.append(Group.relationship_to_user_id_for_group(str(default_uuid), "16777216"))
            expected_added.append(Group.relationship_to_user_id_for_group(str(default_uuid), "51856862"))
        self.assertCountEqual(tracker.tuples_removed, expected_removed)
        self.assertCountEqual(tracker.tuples_added, expected_added)
        self.assertTrue(
            RoleBindingPrincipal.objects.filter(binding=binding, principal=principal, source="direct").exists()
        )

    def test_rewrite_with_no_memberships_only_updates_user_id(self):
        """Principal with no M2M groups/bindings still replicates default-group tuples."""
        principal = Principal.objects.create(
            username="teppo.haapala@apotti.fi", tenant=self.tenant, user_id="16777216"
        )

        tracker = _ReplicationTracker()
        self.assertIsNone(rewrite_principal_user_id(principal, "51856862", tracker))

        principal.refresh_from_db()
        self.assertEqual(principal.user_id, "51856862")
        # Default-group tuples are still replicated even without M2M memberships
        mapping = TenantMapping.objects.get(tenant=self.tenant)
        expected_removed = []
        expected_added = []
        for default_uuid in (mapping.default_group_uuid, mapping.default_admin_group_uuid):
            expected_removed.append(Group.relationship_to_user_id_for_group(str(default_uuid), "16777216"))
            expected_added.append(Group.relationship_to_user_id_for_group(str(default_uuid), "51856862"))
        self.assertCountEqual(tracker.tuples_removed, expected_removed)
        self.assertCountEqual(tracker.tuples_added, expected_added)

    def test_rewrite_raises_when_principal_user_id_missing(self):
        """Rewrite requires an existing user_id; missing ids use the assign/merge path instead."""
        principal = Principal.objects.create(username="jdross@redhat.com", tenant=self.tenant)
        tracker = _ReplicationTracker()
        with self.assertRaises(ValueError):
            rewrite_principal_user_id(principal, "51856862", tracker)

    def test_rewrite_raises_on_cross_tenant_owner_of_new_user_id(self):
        """Refuse to merge an owner of the new user_id from another tenant."""
        other_tenant = Tenant.objects.create(
            tenant_name="other-tenant",
            account_id="99999999",
            org_id="99999999",
            ready=True,
        )
        Principal.objects.create(username="other@example.com", tenant=other_tenant, user_id="51856862")
        principal = Principal.objects.create(
            username="teppo.haapala@apotti.fi", tenant=self.tenant, user_id="16777216"
        )

        tracker = _ReplicationTracker()
        with self.assertRaises(RuntimeError):
            rewrite_principal_user_id(principal, "51856862", tracker)

        principal.refresh_from_db()
        self.assertEqual(principal.user_id, "16777216")

    def test_ensure_principal_rewrites_mismatched_user_id_in_place(self):
        """Ensure path rewrites a stale user_id when the BOP id is free."""
        principal = Principal.objects.create(
            username="teppo.haapala@apotti.fi", tenant=self.tenant, user_id="16777216"
        )
        group = Group.objects.create(name="admins", tenant=self.tenant)
        group.principals.add(principal)

        user = User()
        user.username = "teppo.haapala@apotti.fi"
        user.user_id = 51856862
        user.org_id = self.tenant.org_id

        tracker = _ReplicationTracker()
        result = _ensure_principal_with_user_id_in_tenant(user, self.tenant, replicator=tracker)
        self.assertIsNone(result)

        principal.refresh_from_db()
        self.assertEqual(principal.user_id, "51856862")

        # M2M group tuple
        expected_removed = [Group.relationship_to_user_id_for_group(str(group.uuid), "16777216")]
        expected_added = [Group.relationship_to_user_id_for_group(str(group.uuid), "51856862")]

        # Default-group tuples (via TenantMapping, not M2M)
        mapping = TenantMapping.objects.get(tenant=self.tenant)
        for default_uuid in (mapping.default_group_uuid, mapping.default_admin_group_uuid):
            expected_removed.append(Group.relationship_to_user_id_for_group(str(default_uuid), "16777216"))
            expected_added.append(Group.relationship_to_user_id_for_group(str(default_uuid), "51856862"))

        self.assertCountEqual(tracker.tuples_removed, expected_removed)
        self.assertCountEqual(tracker.tuples_added, expected_added)

    def test_rewrite_removes_default_group_tuples_for_old_user_id(self):
        """Default-group tuples (not in M2M) are removed for old user_id and added for new."""
        principal = Principal.objects.create(
            username="teppo.haapala@apotti.fi", tenant=self.tenant, user_id="16777216"
        )
        mapping = TenantMapping.objects.get(tenant=self.tenant)

        tracker = _ReplicationTracker()
        self.assertIsNone(rewrite_principal_user_id(principal, "51856862", tracker))

        principal.refresh_from_db()
        self.assertEqual(principal.user_id, "51856862")

        default_uuid = str(mapping.default_group_uuid)
        admin_uuid = str(mapping.default_admin_group_uuid)
        expected_removed = [
            Group.relationship_to_user_id_for_group(default_uuid, "16777216"),
            Group.relationship_to_user_id_for_group(admin_uuid, "16777216"),
        ]
        expected_added = [
            Group.relationship_to_user_id_for_group(default_uuid, "51856862"),
            Group.relationship_to_user_id_for_group(admin_uuid, "51856862"),
        ]
        for t in expected_removed:
            self.assertIn(t, tracker.tuples_removed)
        for t in expected_added:
            self.assertIn(t, tracker.tuples_added)

    def test_rewrite_catches_integrity_error_on_concurrent_claim(self):
        """Concurrent claim of user_id between lookup and save is caught and resolved."""
        principal = Principal.objects.create(
            username="teppo.haapala@apotti.fi", tenant=self.tenant, user_id="16777216"
        )

        original_save = Principal.save
        call_count = {"n": 0}

        def flaky_save(self_principal, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1 and self_principal.pk == principal.pk:
                raise IntegrityError(
                    'duplicate key value violates unique constraint "management_principal_user_id_key"'
                )
            return original_save(self_principal, *args, **kwargs)

        tracker = _ReplicationTracker()
        with (
            patch("management.tenant_service.tenant_service._resolve_user_id_conflict") as mock_resolve,
            patch.object(Principal, "save", flaky_save),
        ):
            rewrite_principal_user_id(principal, "51856862", tracker)

        mock_resolve.assert_called_once_with(principal, "51856862", tracker)
