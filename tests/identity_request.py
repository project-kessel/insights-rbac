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
"""Test Case extension to collect common test data."""

import uuid
import os

from base64 import b64encode
from json import dumps as json_dumps
from unittest.mock import Mock

from django.test import TestCase, override_settings, TransactionTestCase
from faker import Faker

from api.models import Tenant
from api.common import RH_IDENTITY_HEADER


class BaseIdentityRequest:
    """Parent Class for IAM test cases."""

    fake = Faker()

    @classmethod
    def setUpClass(cls):
        """Set up each test class."""
        super().setUpClass()
        cls.customer_data = cls._create_customer_data()
        cls.user_data = cls._create_user_data()
        cls.request_context = cls._create_request_context(cls.customer_data, cls.user_data)
        cls.tenant_name = cls.customer_data.get("tenant_name")
        cls.tenant = Tenant(
            tenant_name=cls.tenant_name,
            account_id=cls.customer_data["account_id"],
            org_id=cls.customer_data["org_id"],
            ready=True,
        )
        cls.tenant.save()
        cls.headers = cls.request_context["request"].META

    @classmethod
    def tearDownClass(cls):
        """Tear down the class."""
        # TODO: avoid deletion of cls.tenant tests ?
        if cls.tenant.id is not None:
            cls.tenant.delete()
        super().tearDownClass()

    def tearDown(self):
        """Tear down each test - clear cache to ensure test isolation."""
        from management.utils import PRINCIPAL_CACHE

        try:
            # Try to clear Redis cache if available
            if PRINCIPAL_CACHE.use_caching:
                PRINCIPAL_CACHE.connection.flushdb()
        except Exception:
            # If Redis is not available or fails, just disable caching
            PRINCIPAL_CACHE.disable_caching()
        super().tearDown()

    @classmethod
    def _create_customer_data(cls):
        """Create customer data."""
        account = cls.fake.ean8()
        tenant = f"acct{account}"
        org_id = cls.fake.ean8()
        customer = {"account_id": account, "tenant_name": tenant, "org_id": org_id}
        return customer

    @classmethod
    def _create_user_data(cls):
        """Create user data with unique username to avoid cache collisions between tests."""
        # Generate unique username by appending UUID to avoid cache collisions
        base_username = cls.fake.user_name()
        unique_username = f"{base_username}_{uuid.uuid4().hex[:8]}"
        user_data = {"username": unique_username, "email": cls.fake.email(), "user_id": cls.fake.ean8()}
        return user_data

    def _create_service_account_data(cls) -> dict[str, str]:
        """Create service account data"""
        client_id = str(uuid.uuid4())
        return {"client_id": client_id, "username": f"service-account-{client_id}"}

    @classmethod
    def _create_request_context(
        cls,
        customer_data: dict[str, str],
        user_data: dict[str, str] = None,
        is_org_admin: bool = True,
        is_internal: bool = False,
        cross_account: bool = False,
        service_account_data: dict[str, str] = None,
    ):
        """Create the request context for a user."""
        customer = customer_data
        account = customer.get("account_id")
        org_id = customer.get("org_id", None)

        identity = cls._build_identity(
            user_data, account, org_id, is_org_admin, is_internal, service_account_data=service_account_data
        )
        if cross_account:
            identity["identity"]["internal"] = {"cross_access": True}
        json_identity = json_dumps(identity)
        mock_header = b64encode(json_identity.encode("utf-8"))
        request = Mock()
        request.headers = {RH_IDENTITY_HEADER: mock_header}
        request.META = {RH_IDENTITY_HEADER: mock_header}
        request.scope = {}
        request_context = {"request": request}
        return request_context

    @classmethod
    def _build_identity(
        cls,
        user_data: dict[str, str],
        account: str,
        org_id: str,
        is_org_admin: bool,
        is_internal: bool,
        service_account_data: dict[str, str] = None,
    ):
        identity = {"identity": {"account_number": account, "org_id": org_id}}
        if user_data is not None:
            identity["identity"]["user"] = {
                "username": user_data.get("username"),
                "email": user_data.get("email"),
                "is_org_admin": is_org_admin,
                "user_id": "1111111",
            }

        if service_account_data:
            identity["identity"]["service_account"] = {
                "client_id": service_account_data.get("client_id"),
                "username": service_account_data.get("username"),
            }

        if is_internal:
            identity["identity"]["type"] = "Associate"
            identity["identity"]["associate"] = identity.get("identity").get("user")
            identity["identity"]["user"]["is_internal"] = True
        else:
            if user_data:
                identity["identity"]["type"] = "User"
            else:
                identity["identity"]["type"] = "ServiceAccount"

        return identity


@override_settings(REPLICATION_TO_RELATION_ENABLED=True, PRINCIPAL_USER_DOMAIN="redhat")
class IdentityRequest(BaseIdentityRequest, TestCase):
    pass


@override_settings(REPLICATION_TO_RELATION_ENABLED=True, PRINCIPAL_USER_DOMAIN="redhat")
class TransactionalIdentityRequest(BaseIdentityRequest, TransactionTestCase):
    def setUp(self):
        super().setUp()

        Tenant.objects.get_or_create(tenant_name="public")
