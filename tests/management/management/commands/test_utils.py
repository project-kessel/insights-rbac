from datetime import datetime
from unittest.mock import mock_open, patch

from django.db.utils import IntegrityError
from django.test import TestCase

from api.models import Tenant
from management.management.commands.utils import (
    populate_tenant_user_data,
    populate_service_account_data,
    process_batch,
    populate_workspace_data,
    batch_import_workspace,
)
from management.models import Principal
from management.principal.model import SERVICE_ACCOUNT_USERNAME_FORMAT
from management.tenant_mapping.model import TenantMapping
from management.workspace.model import Workspace


class TestProcessBatch(TestCase):
    @patch("management.management.commands.utils.process_batch")
    def test_populate_tenant_user_data(self, batch_mock):
        mock_file_content = """orgs_info[0].id,orgs_info[0].perm[0],principals[0],_id
1000000,admin:org:all,test_user_1,1
10000001,admin:org:all,test_user_2,2
10000002,,test_user_3,3
10000003,,test_user_4,4
"""

        with patch("builtins.open", mock_open(read_data=mock_file_content)):
            populate_tenant_user_data("file_name", start_line=2, batch_size=2)

        self.assertEqual(
            batch_mock.call_args_list[0][0][0],
            [
                ("10000001", True, "test_user_2", "2"),
                ("10000002", False, "test_user_3", "3"),
            ],
        )
        self.assertEqual(
            batch_mock.call_args_list[1][0][0],
            [
                ("10000003", False, "test_user_4", "4"),
            ],
        )

    @patch("management.management.commands.utils.BOOT_STRAP_SERVICE")
    def test_process_batch(self, mock_bss):
        username = "test_user"
        user_id = "u1"
        org_id = "o1"
        is_admin = True
        batch = [("o1", True, "test_user", "u1")]
        process_batch(batch)
        user = mock_bss.import_bulk_users.call_args[0][0][0]
        self.assertEqual(user.username, username)
        self.assertEqual(user.user_id, user_id)
        self.assertEqual(user.org_id, org_id)
        self.assertEqual(user.admin, is_admin)

    @patch("management.management.commands.utils.BOOT_STRAP_SERVICE")
    def test_retrying_bulk(self, mock_bss):
        mock_bss.import_bulk_users.side_effect = [
            IntegrityError("IntegrityError: duplicate key value violates unique constraint"),
            None,
        ]

        batch = [("o1", True, "test_user", "u1")]
        process_batch(batch)
        self.assertEqual(mock_bss.import_bulk_users.call_count, 2)

    def test_new_tenants_are_not_ready(self):
        mock_file_content = """orgs_info[0].id,orgs_info[0].perm[0],principals[0],_id
1000000,admin:org:all,test_user_1,1
10000001,admin:org:all,test_user_2,2
"""
        with patch("builtins.open", mock_open(read_data=mock_file_content)):
            populate_tenant_user_data("file_name")

        self.assertFalse(Tenant.objects.get(org_id="1000000").ready)
        self.assertFalse(Tenant.objects.get(org_id="10000001").ready)
        self.assertEqual(2, Tenant.objects.exclude(tenant_name="public").count())

    def test_existing_unready_tenants_are_kept_unready_but_still_bootstrapped(self):
        Tenant.objects.create(org_id="1000000", ready=False)
        Tenant.objects.create(org_id="10000001", ready=False)
        mock_file_content = """orgs_info[0].id,orgs_info[0].perm[0],principals[0],_id
1000000,admin:org:all,test_user_1,1
10000001,admin:org:all,test_user_2,2
10000002,,test_user_3,3
"""
        with patch("builtins.open", mock_open(read_data=mock_file_content)):
            populate_tenant_user_data("file_name")

        self.assertFalse(Tenant.objects.get(org_id="1000000").ready)
        self.assertFalse(Tenant.objects.get(org_id="10000001").ready)
        self.assertFalse(Tenant.objects.get(org_id="10000002").ready)

        self.assertTrue(TenantMapping.objects.filter(tenant__org_id="1000000").exists())
        self.assertTrue(TenantMapping.objects.filter(tenant__org_id="10000001").exists())
        self.assertTrue(TenantMapping.objects.filter(tenant__org_id="10000002").exists())

    def test_existing_ready_tenants_are_kept_ready_but_still_bootstrapped(self):
        Tenant.objects.create(org_id="1000000", ready=True)
        Tenant.objects.create(org_id="10000001", ready=True)
        mock_file_content = """orgs_info[0].id,orgs_info[0].perm[0],principals[0],_id
1000000,admin:org:all,test_user_1,1
10000001,admin:org:all,test_user_2,2
10000002,,test_user_3,3
"""
        with patch("builtins.open", mock_open(read_data=mock_file_content)):
            populate_tenant_user_data("file_name")

        self.assertTrue(Tenant.objects.get(org_id="1000000").ready)
        self.assertTrue(Tenant.objects.get(org_id="10000001").ready)
        self.assertFalse(Tenant.objects.get(org_id="10000002").ready)

        self.assertTrue(TenantMapping.objects.filter(tenant__org_id="1000000").exists())
        self.assertTrue(TenantMapping.objects.filter(tenant__org_id="10000001").exists())
        self.assertTrue(TenantMapping.objects.filter(tenant__org_id="10000002").exists())

    def test_import_service_account_data(self):
        client_id_1 = "8c22358-c2ab-40cc-bbc1-e4eff3exxb37xx"
        client_id_2 = "1421687f3-2bc0-4128-9d52-b92b9a22a631"
        client_id_not_in_file = "1421cc7f3-2bc0-4128-9d52-b92bddd2a631"
        user_id_1 = "b6333341-f028-4f29-852e-375132644bcc"
        user_id_2 = "181sdf16-414d-48dd-80a1-264df5d4ffd1"
        user_id_3 = "18xx2f16-414d-48dd-80a1-24df5cccffd1"
        tenant_1 = Tenant.objects.create(tenant_name="test_tenant_1")
        tenant_2 = Tenant.objects.create(tenant_name="test_tenant_2")
        mock_file_content = f"""user_id,client_id
{user_id_1},{client_id_1}
133331a4-10f3-4e84-83de-48b91f8faxx9,1b49xxc18-9915-40e2-972a-c8759632ac59
a210f23c-f2d2-40c6-b47c-43fa1bgg814a,0dffe7e11-c56e-4fcb-b7a6-66db2e013983
212cdcfe-e332-445c-bc35-4cc0xx8391d2,164x0f206-1161-446c-8bfd-b05039feec71
{user_id_2},{client_id_2}
"""
        Principal.objects.bulk_create(
            [
                Principal(
                    username=SERVICE_ACCOUNT_USERNAME_FORMAT.format(clientId=client_id_1),
                    service_account_id=client_id_1,
                    type=Principal.Types.SERVICE_ACCOUNT,
                    tenant=tenant_1,
                ),
                Principal(
                    username=SERVICE_ACCOUNT_USERNAME_FORMAT.format(clientId=client_id_2),
                    service_account_id=client_id_2,
                    user_id="wrong_user_id",
                    type=Principal.Types.SERVICE_ACCOUNT,
                    tenant=tenant_2,
                ),
                Principal(
                    username=SERVICE_ACCOUNT_USERNAME_FORMAT.format(clientId=client_id_not_in_file),
                    service_account_id=client_id_not_in_file,
                    user_id=user_id_3,
                    type=Principal.Types.SERVICE_ACCOUNT,
                    tenant=tenant_2,
                ),
            ]
        )
        with patch("builtins.open", mock_open(read_data=mock_file_content)):
            populate_service_account_data("file_name")

        self.assertEqual(Principal.objects.get(service_account_id=client_id_1).user_id, user_id_1)
        self.assertEqual(Principal.objects.get(service_account_id=client_id_2).user_id, user_id_2)
        self.assertEqual(Principal.objects.get(service_account_id=client_id_not_in_file).user_id, user_id_3)

    def test_import_uppercase_username_matches_lowercase_principal_username(self):
        mock_file_content = """orgs_info[0].id,orgs_info[0].perm[0],principals[0],_id
1000000,admin:org:all,TEST_USER_1,1
10000001,admin:org:all,TEST_USER_2,2
"""

        # Create existing users with lowercased usernames
        tenant_1 = Tenant.objects.create(org_id="1000000", tenant_name="test_tenant_1", ready=True)
        tenant_2 = Tenant.objects.create(org_id="10000001", tenant_name="test_tenant_2", ready=True)
        Principal.objects.bulk_create(
            [
                Principal(username="test_user_1", tenant=tenant_1),
                Principal(username="test_user_2", tenant=tenant_2),
            ]
        )

        with patch("builtins.open", mock_open(read_data=mock_file_content)):
            populate_tenant_user_data("file_name")

        self.assertEqual(Principal.objects.get(username="test_user_1").user_id, "1")
        self.assertEqual(Principal.objects.get(username="test_user_2").user_id, "2")

    @patch("management.management.commands.utils.batch_import_workspace")
    def test_populate_workspace_data(self, batch_mock):
        mock_file_content = """id,account,org_id,name,ungrouped,created_on,modified_on
47cd5563-0f55-4624-a182-9a69fa307c63,123456,987654,test_group_0,False,2025-03-18 15:19:53.509203+00:00,2025-03-18 15:19:53.509206+00:00
7c5e11d2-0bda-4f90-ac78-2f99fc572573,123456,987654,test_group_1,False,2025-03-18 15:19:53.516576+00:00,2025-03-18 15:19:53.516579+00:00
2c8b9e47-7c8a-40cc-81ed-326c5a41b045,123456,987654,test_group_2,False,2025-03-18 15:19:53.523742+00:00,2025-03-18 15:19:53.523743+00:00
"""

        with patch("builtins.open", mock_open(read_data=mock_file_content)):
            populate_workspace_data("file_name", batch_size=2)

        self.assertEqual(
            batch_mock.call_args_list[0][0][0],
            [
                {
                    "account": "123456",
                    "created_on": "2025-03-18 15:19:53.509203+00:00",
                    "id": "47cd5563-0f55-4624-a182-9a69fa307c63",
                    "modified_on": "2025-03-18 15:19:53.509206+00:00",
                    "name": "test_group_0",
                    "org_id": "987654",
                    "ungrouped": "False",
                },
                {
                    "account": "123456",
                    "created_on": "2025-03-18 15:19:53.516576+00:00",
                    "id": "7c5e11d2-0bda-4f90-ac78-2f99fc572573",
                    "modified_on": "2025-03-18 15:19:53.516579+00:00",
                    "name": "test_group_1",
                    "org_id": "987654",
                    "ungrouped": "False",
                },
            ],
        )
        self.assertEqual(
            batch_mock.call_args_list[1][0][0],
            [
                {
                    "account": "123456",
                    "created_on": "2025-03-18 15:19:53.523742+00:00",
                    "id": "2c8b9e47-7c8a-40cc-81ed-326c5a41b045",
                    "modified_on": "2025-03-18 15:19:53.523743+00:00",
                    "name": "test_group_2",
                    "org_id": "987654",
                    "ungrouped": "False",
                }
            ],
        )

    @patch("management.management.commands.utils.BOOT_STRAP_SERVICE.create_workspace_relationships")
    def test_batch_import_workspace(self, mock_cwr):
        org_id_1 = "987654"
        workspace_id_1 = "47cd5563-0f55-4624-a182-9a69fa307c63"
        org_id_2 = "456789"
        workspace_id_2 = "7c5e11d2-0bda-4f90-ac78-2f99fc572573"
        org_id_3 = "123456"
        workspace_id_3 = "2c8b9e47-7c8a-40cc-81ed-326c5a41b045"
        records = [
            {
                "account": "123456",
                "created_on": "2025-03-18 15:19:53.509203+00:00",
                "id": workspace_id_1,
                "modified_on": "2025-03-18 15:19:53.509206+00:00",
                "name": "test_group_0",
                "org_id": org_id_1,
                "ungrouped": "False",
            },
            {
                "account": "123456",
                "created_on": "2025-03-18 15:19:53.509203+00:00",
                "id": workspace_id_2,
                "modified_on": "2025-03-18 15:19:53.509206+00:00",
                "name": "test_group_1",
                "org_id": org_id_2,
                "ungrouped": "True",
            },
            {
                "account": "123456",
                "created_on": "2025-03-18 15:19:53.509203+00:00",
                "id": workspace_id_3,
                "modified_on": "2025-03-18 15:19:53.509206+00:00",
                "name": "test_group_2",
                "org_id": org_id_3,
                "ungrouped": "True",
            },
        ]
        tenants = Tenant.objects.bulk_create(
            [
                Tenant(org_id=org_id_1),
                Tenant(org_id=org_id_2),
            ]
        )
        roots = Workspace.objects.bulk_create(
            [
                Workspace(
                    name="root",
                    tenant=tenants[0],
                    type=Workspace.Types.ROOT,
                ),
                Workspace(
                    name="root",
                    tenant=tenants[1],
                    type=Workspace.Types.ROOT,
                ),
            ]
        )
        defaults = Workspace.objects.bulk_create(
            [
                Workspace(name="default", tenant=tenants[0], type=Workspace.Types.DEFAULT, parent=roots[0]),
                Workspace(name="default", tenant=tenants[1], type=Workspace.Types.DEFAULT, parent=roots[1]),
            ]
        )
        batch_import_workspace(records)
        default_ws_3 = Workspace.objects.default(tenant=Tenant.objects.get(org_id=org_id_3))
        self.assertEqual(
            mock_cwr.call_args[0][0],
            [
                (workspace_id_1, str(defaults[0].id)),
                (workspace_id_2, str(defaults[1].id)),
                (workspace_id_3, str(default_ws_3.id)),
            ],
        )
        self.assertEqual(Workspace.objects.filter(id__in=[workspace_id_1, workspace_id_2, workspace_id_3]).count(), 3)
        self.assertEqual(Workspace.objects.get(id=workspace_id_1).parent, defaults[0])
        self.assertEqual(Workspace.objects.get(id=workspace_id_2).parent, defaults[1])
        self.assertEqual(Workspace.objects.get(id=workspace_id_3).parent, default_ws_3)
        self.assertEqual(Workspace.objects.get(id=workspace_id_2).type, Workspace.Types.UNGROUPED_HOSTS)
        self.assertEqual(Workspace.objects.get(id=workspace_id_2).name, "Ungrouped Hosts")
        self.assertEqual(Workspace.objects.get(id=workspace_id_3).type, Workspace.Types.UNGROUPED_HOSTS)
        self.assertEqual(Workspace.objects.get(id=workspace_id_3).name, "Ungrouped Hosts")
        # Should be idempotent
        updated_name = "updated_name"
        updated_time = "2026-03-18 15:19:53.509206+00:00"
        records[0]["name"] = updated_name
        records[0]["modified_on"] = updated_time
        records[1]["name"] = updated_name
        records[1]["modified_on"] = updated_time
        mock_cwr.reset_mock()
        batch_import_workspace(records)
        self.assertEqual(
            mock_cwr.call_args[0][0],
            [
                (workspace_id_1, str(defaults[0].id)),
                (workspace_id_2, str(defaults[1].id)),
                (workspace_id_3, str(default_ws_3.id)),
            ],
        )
        updated_ws_1 = Workspace.objects.get(id=workspace_id_1)
        self.assertEqual(updated_ws_1.name, updated_name)
        self.assertEqual(updated_ws_1.modified, datetime.fromisoformat(updated_time))
        updated_ws_2 = Workspace.objects.get(id=workspace_id_2)
        self.assertEqual(updated_ws_2.name, "Ungrouped Hosts")
        self.assertEqual(updated_ws_2.modified, datetime.fromisoformat(updated_time))
