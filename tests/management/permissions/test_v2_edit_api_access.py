from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from management.permissions.v2_edit_api_access import is_v2_access_check_required_for_request
from management.tenant_mapping.v2_activation import ensure_v2_write_activated
from tests.v2_util import bootstrap_tenant_for_v2_test

from api.models import Tenant


@override_settings(V2_STRICT_ACCESS_CHECK_FLAG_APPLICATION_NAMES=["strict_app"])
class AccessCheckRequiredTest(TestCase):
    def setUp(self):
        super().setUp()

        self.tenant = Tenant.objects.create(tenant_name="a tenant", org_id="a-tenant")
        bootstrap_tenant_for_v2_test(self.tenant)

        self._set_edit_flag(False)
        self._set_strict_check_flag(False)

    def _check_for(self, applications: list[str]) -> bool:
        request_mock = MagicMock()
        request_mock.tenant = self.tenant
        request_mock.user.org_id = self.tenant.org_id

        return is_v2_access_check_required_for_request(request_mock, applications)

    def _set_edit_flag(self, value: bool):
        self.enterContext(
            patch("management.permissions.v2_edit_api_access.FEATURE_FLAGS.is_v2_edit_api_enabled", return_value=value)
        )

    def _set_strict_check_flag(self, value: bool):
        self.enterContext(
            patch(
                "management.permissions.v2_edit_api_access.FEATURE_FLAGS.is_v2_strict_access_check_enabled",
                return_value=value,
            )
        )

    def test_not_required(self):
        self._set_strict_check_flag(True)

        # other_app is not a strict app, so the flag being set should be irrelevant.
        self.assertFalse(self._check_for(["other_app"]))

    def test_required_if_v2_tenant(self):
        ensure_v2_write_activated(self.tenant)
        self.assertTrue(self._check_for(["other_app"]))

    def test_required_if_edit_enabled(self):
        self._set_edit_flag(True)
        self._set_strict_check_flag(True)

        self.assertTrue(self._check_for(["other_app"]))

    def test_required_for_strict_app_if_strict_enabled(self):
        self._set_strict_check_flag(True)
        self.assertTrue(self._check_for(["strict_app"]))

    def test_required_if_any_required(self):
        self._set_strict_check_flag(True)
        self.assertTrue(self._check_for(["strict_app", "other_app"]))
