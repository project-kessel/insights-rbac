#
# Copyright 2019 Red Hat, Inc.
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
"""Test the principal cleaner."""

from functools import partial
from threading import Event, Thread
import uuid
import json

from unittest.mock import MagicMock, patch, Mock

from django.db import connections, transaction
from django.test import override_settings
from prometheus_client import REGISTRY
from rest_framework import status

from management.group.definer import seed_group
from management.group.model import Group
from management.policy.model import Policy
from management.principal.cleaner import LOCK_ID, clean_tenant_principals
from management.principal.model import Principal
from management.principal.cleaner import (
    process_principal_events_from_kafka,
    METRIC_KAFKA_MESSAGES_SUCCESS_TOTAL,
    METRIC_KAFKA_MESSAGES_FAILURE_TOTAL,
)
from management.principal.proxy import external_principal_to_user
from management.relation_replicator.relation_replicator import PartitionKey, ReplicationEvent, ReplicationEventType
from management.tenant_mapping.model import TenantMapping
from management.tenant_service import get_tenant_bootstrap_service
from management.workspace.model import Workspace
from api.models import Tenant, User
from management.relation_replicator.types import ObjectReference, ObjectType, SubjectReference
from migration_tool.in_memory_tuples import (
    InMemoryRelationReplicator,
    InMemoryTuples,
    RelationTuple,
    all_of,
    relation,
    resource,
    subject,
)
from tests.identity_request import IdentityRequest


class PrincipalCleanerTests(IdentityRequest):
    """Test the principal cleaner functions."""

    def setUp(self):
        """Set up the principal cleaner tests."""
        super().setUp()
        self.group = Group(name="groupA", tenant=self.tenant)
        self.group.save()

    def test_principal_cleanup_none(self):
        """Test that we can run a principal clean up on a tenant with no principals."""
        try:
            clean_tenant_principals(self.tenant)
        except Exception:
            self.fail(msg="clean_tenant_principals encountered an exception")
        self.assertEqual(Principal.objects.count(), 0)

    @patch(
        "management.principal.proxy.PrincipalProxy._request_principals",
        return_value={"status_code": status.HTTP_200_OK, "data": []},
    )
    def test_principal_cleanup_skip_cross_account_principals(self, mock_request):
        """Test that principal clean up on a tenant will skip cross account principals."""
        Principal.objects.create(username="user1", tenant=self.tenant)
        Principal.objects.create(username="CAR", cross_account=True, tenant=self.tenant)
        self.assertEqual(Principal.objects.count(), 2)

        try:
            clean_tenant_principals(self.tenant)
        except Exception:
            self.fail(msg="clean_tenant_principals encountered an exception")
        self.assertEqual(Principal.objects.count(), 1)

    @patch(
        "management.principal.proxy.PrincipalProxy._request_principals",
        return_value={"status_code": status.HTTP_200_OK, "data": []},
    )
    def test_principal_cleanup_skips_service_account_principals(self, mock_request):
        """Test that principal clean up on a tenant will skip service account principals."""
        # Create a to-be-removed user principal and a service account that should be left untouched.
        service_account_client_id = str(uuid.uuid4())
        Principal.objects.create(username="regular user", tenant=self.tenant)
        Principal.objects.create(
            username=f"service-account-{service_account_client_id}",
            service_account_id=service_account_client_id,
            tenant=self.tenant,
            type="service-account",
        )
        self.assertEqual(Principal.objects.count(), 2)

        try:
            clean_tenant_principals(self.tenant)
        except Exception:
            self.fail(msg="clean_tenant_principals encountered an exception")

        # Assert that the only principal left for the tenant is the service account, which should have been left
        # untouched.
        self.assertEqual(Principal.objects.count(), 1)

        service_account = Principal.objects.all().filter(type="service-account").first()
        self.assertEqual(service_account.service_account_id, service_account_client_id)
        self.assertEqual(service_account.type, "service-account")
        self.assertEqual(service_account.username, f"service-account-{service_account_client_id}")

    @patch(
        "management.principal.proxy.PrincipalProxy._request_principals",
        return_value={"status_code": status.HTTP_200_OK, "data": []},
    )
    def test_principal_cleanup_principal_in_group(self, mock_request):
        """Test that we can run a principal clean up on a tenant with a principal in a group."""
        self.principal = Principal(username="user1", tenant=self.tenant)
        self.principal.save()
        self.group.principals.add(self.principal)
        self.group.save()
        try:
            clean_tenant_principals(self.tenant)
        except Exception:
            self.fail(msg="clean_tenant_principals encountered an exception")
        self.assertEqual(Principal.objects.count(), 0)

    @patch(
        "management.principal.proxy.PrincipalProxy._request_principals",
        return_value={"status_code": status.HTTP_200_OK, "data": []},
    )
    def test_principal_cleanup_principal_not_in_group(self, mock_request):
        """Test that we can run a principal clean up on a tenant with a principal not in a group."""
        self.principal = Principal(username="user1", tenant=self.tenant)
        self.principal.save()
        try:
            clean_tenant_principals(self.tenant)
        except Exception:
            self.fail(msg="clean_tenant_principals encountered an exception")
        self.assertEqual(Principal.objects.count(), 0)

    @patch(
        "management.principal.proxy.PrincipalProxy._request_principals",
        return_value={"status_code": status.HTTP_200_OK, "data": [{"username": "user1"}]},
    )
    def test_principal_cleanup_principal_exists(self, mock_request):
        """Test that we can run a principal clean up on a tenant with an existing principal."""
        self.principal = Principal(username="user1", tenant=self.tenant)
        self.principal.save()
        try:
            clean_tenant_principals(self.tenant)
        except Exception:
            self.fail(msg="clean_tenant_principals encountered an exception")
        self.assertEqual(Principal.objects.count(), 1)

    @patch(
        "management.principal.proxy.PrincipalProxy._request_principals",
        return_value={"status_code": status.HTTP_504_GATEWAY_TIMEOUT},
    )
    def test_principal_cleanup_principal_error(self, mock_request):
        """Test that we can handle a principal clean up with an unexpected error from proxy."""
        self.principal = Principal(username="user1", tenant=self.tenant)
        self.principal.save()
        try:
            clean_tenant_principals(self.tenant)
        except Exception:
            self.fail(msg="clean_tenant_principals encountered an exception")
        self.assertEqual(Principal.objects.count(), 1)


# Kafka JSON message format (converted from XML)
KAFKA_MESSAGE_BODY = json.dumps(
    {
        "CanonicalMessage": {
            "Header": {
                "System": "WEB",
                "Operation": "update",
                "Type": "User",
                "InstanceId": "660a018a6d336076b5b57fff",
                "Timestamp": "2024-03-31T20:36:27.820",
            },
            "Payload": {
                "Sync": {
                    "User": {
                        "CreatedDate": "2024-02-16T02:57:51.738",
                        "LastUpdatedDate": "2024-02-21T06:47:24.672",
                        "Identifiers": {
                            "Identifier": [
                                {"system": "WEB", "entity-name": "User", "qualifier": "id", "text": "56780000"},
                                {"system": "FOO", "entity-name": "User", "qualifier": "id", "text": "56780001"},
                            ],
                            "Reference": [
                                {"system": "WEB", "entity-name": "Customer", "qualifier": "id", "text": "17685860"},
                                {"system": "EBS", "entity-name": "Account", "qualifier": "number", "text": "11111111"},
                            ],
                        },
                        "Status": {"State": "Inactive"},
                        "Person": {
                            "FirstName": "Test",
                            "LastName": "Principal",
                            "Salutation": "Mr.",
                            "Title": "QE",
                            "Credentials": {"Login": "principal-test"},
                        },
                    }
                }
            },
        }
    }
)

KAFKA_MESSAGE_CREATION = json.dumps(
    {
        "CanonicalMessage": {
            "Header": {
                "System": "WEB",
                "Operation": "insert",
                "Type": "User",
                "InstanceId": "660a018a6d336076b5b57fff",
                "Timestamp": "2024-03-31T20:36:27.820",
            },
            "Payload": {
                "Sync": {
                    "User": {
                        "CreatedDate": "2024-02-16T02:57:51.738",
                        "LastUpdatedDate": "2024-02-21T06:47:24.672",
                        "Identifiers": {
                            "Identifier": {
                                "system": "WEB",
                                "entity-name": "User",
                                "qualifier": "id",
                                "text": "56780000",
                            },
                            "Reference": [
                                {"system": "WEB", "entity-name": "Customer", "qualifier": "id", "text": "17685860"},
                                {"system": "EBS", "entity-name": "Account", "qualifier": "number", "text": "11111111"},
                            ],
                        },
                        "Status": {"State": "Active"},
                        "Person": {
                            "FirstName": "Test",
                            "LastName": "Principal",
                            "Salutation": "Mr.",
                            "Title": "QE",
                            "Credentials": {"Login": "principal-test"},
                        },
                    }
                }
            },
        }
    }
)

KAFKA_MESSAGE_SPECIAL = json.dumps(
    {
        "CanonicalMessage": {
            "Header": {
                "System": "WEB",
                "Operation": "insert",
                "Type": "User",
                "InstanceId": "666a018a6d336076b5b57fff",
                "Timestamp": "2024-11-12T09:48:18.260",
            },
            "Payload": {
                "Sync": {
                    "User": {
                        "CreatedDate": "2024-11-12T09:48:12.978",
                        "LastUpdatedDate": "2024-11-12T09:48:14.336",
                        "Identifiers": {
                            "Identifier": {
                                "system": "WEB",
                                "entity-name": "User",
                                "qualifier": "id",
                                "text": "56780000",
                            },
                            "Reference": {
                                "system": "WEB",
                                "entity-name": "Customer",
                                "qualifier": "id",
                                "text": "17685860",
                            },
                        },
                        "Status": {"State": "Inactive"},
                        "Person": {
                            "FirstName": "Teamnado",
                            "LastName": "Test Automation",
                            "Title": "Test User",
                            "Credentials": {"Login": "principal-test"},
                        },
                    }
                }
            },
        }
    }
)


def create_mock_kafka_message(message_body, partition=0, offset=0):
    """Create a mock Kafka message."""
    mock_message = Mock()
    mock_message.value = message_body
    mock_message.partition = partition
    mock_message.offset = offset
    return mock_message


class PrincipalKafkaTests(IdentityRequest):
    """Test the principal processor functions with Kafka."""

    def setUp(self):
        """Set up the principal processor tests."""
        super().setUp()
        self.principal_name = "principal-test"
        self.principal_user_id = "56780000"
        self.tenant.org_id = "17685860"
        self.tenant.save()
        self.group = Group(name="groupA", tenant=self.tenant)
        self.group.save()

    @patch("management.principal.cleaner.KafkaConsumer")
    def test_principal_cleanup_none(self, consumer_mock):
        """Test that we can run a principal clean up with no messages."""
        before = REGISTRY.get_sample_value(METRIC_KAFKA_MESSAGES_SUCCESS_TOTAL)

        # Mock consumer with no messages
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([])
        consumer_mock.return_value = consumer_instance

        process_principal_events_from_kafka()

        after = REGISTRY.get_sample_value(METRIC_KAFKA_MESSAGES_SUCCESS_TOTAL)
        self.assertTrue(before == after or before is None and after is None)
        consumer_instance.close.assert_called_once()

    @patch(
        "management.principal.proxy.PrincipalProxy._request_principals",
        return_value={
            "status_code": status.HTTP_200_OK,
            "data": [],
        },
    )
    @patch("management.group.model.AccessCache")
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_cleanup_principal_in_or_not_in_group(self, consumer_mock, cache_class, proxy_mock):
        """Test that we can run a principal clean up on a tenant with a principal in a group."""
        principal_name = "principal-test"
        self.principal = Principal(username=principal_name, tenant=self.tenant, user_id="56780000")
        self.principal.save()
        self.group.principals.add(self.principal)
        self.group.save()

        before = REGISTRY.get_sample_value(METRIC_KAFKA_MESSAGES_SUCCESS_TOTAL)

        # Mock consumer with one message
        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_BODY)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        cache_mock = MagicMock()
        cache_class.return_value = cache_mock
        process_principal_events_from_kafka()

        after = REGISTRY.get_sample_value(METRIC_KAFKA_MESSAGES_SUCCESS_TOTAL)
        self.assertFalse(Principal.objects.filter(username=principal_name).exists())
        self.group.refresh_from_db()
        self.assertFalse(self.group.principals.all())
        cache_mock.delete_policy.assert_called_once_with(self.principal.uuid)
        self.assertTrue(before + 1 == after or (before is None and after == 1))

        # When principal not in group
        self.principal = Principal(username=principal_name, tenant=self.tenant, user_id="56780000")
        self.principal.save()

        consumer_instance.__iter__.return_value = iter([mock_message])
        process_principal_events_from_kafka()
        self.assertFalse(Principal.objects.filter(username=principal_name).exists())

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [],
        },
    )
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_cleanup_principal_does_not_exist(self, consumer_mock, proxy_mock):
        """Test that can run a principal clean up with a principal does not exist."""
        principal_name = "principal-keep"
        self.principal = Principal(username=principal_name, tenant=self.tenant)
        self.principal.save()

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_BODY)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        process_principal_events_from_kafka()
        self.assertTrue(Principal.objects.filter(username=principal_name).exists())

        consumer_instance.__iter__.return_value = iter([create_mock_kafka_message(KAFKA_MESSAGE_SPECIAL)])
        process_principal_events_from_kafka()

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "user_id": 56780000,
                    "org_id": "17685860",
                    "username": "principal-test",
                    "email": "test_user@email.com",
                    "first_name": "user",
                    "last_name": "test",
                    "is_org_admin": False,
                    "is_active": True,
                }
            ],
        },
    )
    @patch("management.principal.cleaner.KafkaConsumer")
    @override_settings(PRINCIPAL_CLEANUP_UPDATE_ENABLED_KAFKA=True)
    def test_principal_creation_event_updates_existing_principal(self, consumer_mock, proxy_mock):
        """Test that we can run principal creation event."""
        public_tenant = Tenant.objects.get(tenant_name="public")
        Group.objects.create(name="default", platform_default=True, tenant=public_tenant)

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_CREATION)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        tenant = Tenant.objects.get(org_id="17685860")
        Principal.objects.create(tenant=tenant, username="principal-test")
        process_principal_events_from_kafka()

        consumer_instance.close.assert_called_once()
        self.assertTrue(Tenant.objects.filter(org_id="17685860").exists())
        self.assertTrue(Principal.objects.filter(user_id=self.principal_user_id).exists())

    @patch("management.principal.cleaner.retrieve_user_info")
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_failure_processing_message(self, consumer_mock, retrieve_user_mock):
        """Test failure handling when processing message."""
        principal_name = "principal-test"
        principal = Principal.objects.create(username=principal_name, tenant=self.tenant)
        principal.save()
        self.group.principals.add(principal)
        self.group.save()

        before = REGISTRY.get_sample_value(METRIC_KAFKA_MESSAGES_FAILURE_TOTAL)
        success_before = REGISTRY.get_sample_value(METRIC_KAFKA_MESSAGES_SUCCESS_TOTAL)

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_BODY)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        retrieve_user_mock.side_effect = Exception("Something went wrong")
        process_principal_events_from_kafka()

        after = REGISTRY.get_sample_value(METRIC_KAFKA_MESSAGES_FAILURE_TOTAL)
        success_after = REGISTRY.get_sample_value(METRIC_KAFKA_MESSAGES_SUCCESS_TOTAL)
        self.assertTrue(Principal.objects.filter(username=principal_name).exists())
        self.group.refresh_from_db()
        self.assertTrue(self.group.principals.all())
        self.assertTrue((before + 1 == after) or (before is None and after == 1))
        self.assertTrue(success_before == success_after or (success_before is None and success_after is None))


@override_settings(V2_BOOTSTRAP_TENANT=True, PRINCIPAL_CLEANUP_UPDATE_ENABLED_KAFKA=True)
class PrincipalKafkaTestsWithV2TenantBootstrap(PrincipalKafkaTests):
    """Test the principal processor functions with V2 tenant bootstrap enabled."""

    _tuples: InMemoryTuples

    def setUp(self):
        super().setUp()
        seed_group()
        self._tuples = InMemoryTuples()

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": []},
    )
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_cleanup_same_principal_name_in_multiple_tenants(self, consumer_mock, proxy_mock):
        """Test that can run a principal clean up with a principal that have multiple tenants."""
        another_tenant = Tenant.objects.create(
            tenant_name="another", account_id="11111112", org_id="17685861", ready=True
        )
        self.principal = Principal.objects.create(username=self.principal_name, user_id="56780000", tenant=self.tenant)
        Principal.objects.create(username=self.principal_name, user_id="12340000", tenant=another_tenant)
        self.assertEqual(Principal.objects.filter(username=self.principal_name).count(), 2)

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_BODY)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        process_principal_events_from_kafka()

        consumer_instance.close.assert_called_once()
        self.assertFalse(Principal.objects.filter(username=self.principal_name, tenant=self.tenant).exists())
        self.assertTrue(Principal.objects.filter(username=self.principal_name, tenant=another_tenant).exists())

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": []},
    )
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_cleanup_principal_does_not_exist_no_tenant(self, consumer_mock, proxy_mock):
        """Test cleanup when principal exists but tenant doesn't match."""
        principal_name = "principal-keep"
        # Create principal for a different tenant than what's in the message
        other_tenant = Tenant.objects.create(tenant_name="other", account_id="99999", org_id="99999999", ready=True)
        self.principal = Principal(username=principal_name, tenant=other_tenant)
        self.principal.save()

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_BODY)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        process_principal_events_from_kafka()

        # Principal in other tenant should remain
        self.assertTrue(Principal.objects.filter(username=principal_name, tenant=other_tenant).exists())
        consumer_instance.close.assert_called_once()

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "user_id": 56780000,
                    "org_id": "17685860",
                    "username": "principal-test",
                    "email": "test_user@email.com",
                    "first_name": "user",
                    "last_name": "test",
                    "is_org_admin": False,
                    "is_active": True,
                }
            ],
        },
    )
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_principal_creation_event_bootstraps_new_tenant(self, consumer_mock, proxy_mock):
        """Test that principal creation event creates a new tenant."""
        # Ensure tenant doesn't exist initially
        Tenant.objects.filter(org_id="17685860").delete()

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_CREATION)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        process_principal_events_from_kafka()

        consumer_instance.close.assert_called_once()
        self.assertTrue(Tenant.objects.filter(org_id="17685860").exists())
        tenant = Tenant.objects.get(org_id="17685860")
        self.assertTrue(Principal.objects.filter(user_id=self.principal_user_id, tenant=tenant).exists())

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "user_id": 56780000,
                    "org_id": "17685860",
                    "username": "principal-test",
                    "email": "test_user@email.com",
                    "first_name": "user",
                    "last_name": "test",
                    "is_org_admin": False,
                    "is_active": True,
                }
            ],
        },
    )
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_principal_creation_event_bootstraps_existing_tenants(self, consumer_mock, proxy_mock):
        """Test that principal creation event bootstraps existing unready tenant."""
        tenant = Tenant.objects.get(org_id="17685860")
        tenant.ready = False
        tenant.save()

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_CREATION)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        process_principal_events_from_kafka()

        consumer_instance.close.assert_called_once()
        tenant.refresh_from_db()
        self.assertTrue(tenant.ready)
        self.assertTrue(Principal.objects.filter(user_id=self.principal_user_id, tenant=tenant).exists())

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "user_id": 56780000,
                    "org_id": "17685860",
                    "username": "principal-test",
                    "email": "test_user@email.com",
                    "first_name": "user",
                    "last_name": "test",
                    "is_org_admin": False,
                    "is_active": True,
                }
            ],
        },
    )
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_principal_creation_event_does_not_bootstrap_already_bootstraped_tenant(self, consumer_mock, proxy_mock):
        """Test that already bootstrapped tenant stays ready."""
        tenant = Tenant.objects.get(org_id="17685860")
        tenant.ready = True
        tenant.save()

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_CREATION)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        process_principal_events_from_kafka()

        consumer_instance.close.assert_called_once()
        tenant.refresh_from_db()
        self.assertTrue(tenant.ready)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": []},
    )
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_principal_creation_event_does_not_create_principal(self, consumer_mock, proxy_mock):
        """Test that principal creation doesn't create principal when proxy returns empty."""
        Tenant.objects.filter(org_id="17685860").delete()

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_CREATION)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        process_principal_events_from_kafka()

        consumer_instance.close.assert_called_once()
        # Tenant should still be created even if principal fetch fails
        self.assertTrue(Tenant.objects.filter(org_id="17685860").exists())
        # But principal shouldn't exist
        self.assertFalse(Principal.objects.filter(username=self.principal_name).exists())

    @patch("management.principal.proxy.PrincipalProxy.request_filtered_principals", return_value={"status_code": 500})
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_principal_creation_event_does_not_create_principal_nor_tenant(self, consumer_mock, proxy_mock):
        """Test that nothing is created when proxy returns error."""
        Tenant.objects.filter(org_id="17685860").delete()

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_CREATION)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        process_principal_events_from_kafka()

        consumer_instance.close.assert_called_once()
        # Nothing should be created on proxy error
        self.assertFalse(Tenant.objects.filter(org_id="17685860").exists())
        self.assertFalse(Principal.objects.filter(username=self.principal_name).exists())

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={
            "status_code": 200,
            "data": [
                {
                    "user_id": 56780000,
                    "org_id": "17685860",
                    "username": "principal-test",
                    "email": "test_user@email.com",
                    "first_name": "user",
                    "last_name": "test",
                    "is_org_admin": False,
                    "is_active": False,  # Disabled user
                }
            ],
        },
    )
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_principal_creation_event_disabled(self, consumer_mock, proxy_mock):
        """Test that disabled user in creation event doesn't create active principal."""
        Tenant.objects.filter(org_id="17685860").delete()

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_CREATION)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        process_principal_events_from_kafka()

        consumer_instance.close.assert_called_once()
        tenant = Tenant.objects.get(org_id="17685860")
        # Principal should exist but not be active
        principal = Principal.objects.get(username=self.principal_name, tenant=tenant)
        self.assertFalse(principal.is_active)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": []},
    )
    @patch("management.group.model.AccessCache")
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_disable_principal_which_is_in_or_not_in_group(self, consumer_mock, cache_class, proxy_mock):
        """Test deleting a principal that is in a group when inactive."""
        principal_name = "principal-test"
        self.principal = Principal(username=principal_name, tenant=self.tenant, user_id="56780000")
        self.principal.save()
        self.group.principals.add(self.principal)
        self.group.save()

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_BODY)  # Inactive state
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        cache_mock = MagicMock()
        cache_class.return_value = cache_mock

        process_principal_events_from_kafka()

        # Principal should be deleted when inactive and not found in proxy
        self.assertFalse(Principal.objects.filter(username=principal_name).exists())
        cache_mock.delete_policy.assert_called_once_with(self.principal.uuid)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": []},
    )
    @patch("management.group.model.AccessCache")
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_disable_principal_without_user_id_in_group(self, consumer_mock, cache_class, proxy_mock):
        """Test deleting a principal without user_id that is in a group when inactive."""
        principal_name = "principal-test"
        # Principal without user_id
        self.principal = Principal(username=principal_name, tenant=self.tenant)
        self.principal.save()
        principal_uuid = self.principal.uuid
        self.group.principals.add(self.principal)
        self.group.save()

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_BODY)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        cache_mock = MagicMock()
        cache_class.return_value = cache_mock

        process_principal_events_from_kafka()

        # Principal should be deleted when inactive and not found in proxy
        self.assertFalse(Principal.objects.filter(username=principal_name).exists())
        cache_mock.delete_policy.assert_called_once_with(principal_uuid)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": []},
    )
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_same_tenant_keeps_ready(self, consumer_mock, proxy_mock):
        """Test that ready tenant stays ready after principal event."""
        tenant = Tenant.objects.get(org_id="17685860")
        tenant.ready = True
        tenant.save()

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_BODY)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        process_principal_events_from_kafka()

        tenant.refresh_from_db()
        self.assertTrue(tenant.ready)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": []},
    )
    @patch("management.principal.cleaner.KafkaConsumer")
    def test_same_tenant_keeps_unready(self, consumer_mock, proxy_mock):
        """Test that unready tenant stays unready after update event."""
        tenant = Tenant.objects.get(org_id="17685860")
        tenant.ready = False
        tenant.save()

        # Update event (not insert) shouldn't bootstrap tenant
        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_BODY)
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        process_principal_events_from_kafka()

        tenant.refresh_from_db()
        self.assertFalse(tenant.ready)

    @patch(
        "management.principal.proxy.PrincipalProxy.request_filtered_principals",
        return_value={"status_code": 200, "data": []},
    )
    @patch("management.principal.cleaner.KafkaConsumer")
    @override_settings(V2_BOOTSTRAP_TENANT=False)
    def test_non_bootstrapped_tenant_no_principal_disabled_user_does_not_produce_replication_event(
        self, consumer_mock, proxy_mock
    ):
        """Test that non-V2 tenant with disabled user doesn't produce replication events."""
        principal_name = "principal-test"
        self.principal = Principal(username=principal_name, tenant=self.tenant, user_id="56780000")
        self.principal.save()

        mock_message = create_mock_kafka_message(KAFKA_MESSAGE_BODY)  # Inactive
        consumer_instance = MagicMock()
        consumer_instance.__iter__.return_value = iter([mock_message])
        consumer_mock.return_value = consumer_instance

        process_principal_events_from_kafka()

        # Principal should be deleted when inactive and not found in proxy
        self.assertFalse(Principal.objects.filter(username=principal_name).exists())
        # No replication events should be produced for non-V2 tenants
        consumer_instance.close.assert_called_once()
