#
# Copyright 2020 Red Hat, Inc.
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

"""Describes the urls and patterns for internal routes."""

from django.urls import path
from internal.integration import views as integration_views
from internal.openapi import openapi

from . import views

integration_urlpatterns = [
    path(
        "api/v1/integrations/tenant/",
        integration_views.TenantViewSet.as_view({"get": "list"}),
        name="integration-tenants",
    ),
    path(
        "api/v1/integrations/tenant/<str:org_id>/roles/",
        integration_views.TenantViewSet.as_view({"get": "roles"}),
        name="integration-roles",
    ),
    path(
        "api/v1/integrations/tenant/<str:org_id>/groups/",
        integration_views.TenantViewSet.as_view({"get": "groups"}),
        name="integration-groups",
    ),
    path(
        "api/v1/integrations/tenant/<str:org_id>/groups/<str:uuid>/roles/",
        integration_views.TenantViewSet.as_view({"get": "roles_for_group"}),
        name="integration-group-roles",
    ),
    path(
        "api/v1/integrations/tenant/<str:org_id>/groups/<str:uuid>/principals/",
        integration_views.TenantViewSet.as_view({"get": "principals_for_group"}),
        name="integration-group-principals",
    ),
    path(
        "api/v1/integrations/tenant/<str:org_id>/principal/<str:principals>/groups/",
        integration_views.TenantViewSet.as_view({"get": "groups_for_principal"}),
        name="integration-princ-groups",
    ),
    path(
        "api/v1/integrations/tenant/<str:org_id>/principal/<str:principals>/groups/<str:uuid>/roles/",
        integration_views.TenantViewSet.as_view({"get": "roles_for_group_principal"}),
        name="integration-princ-roles",
    ),
    path("api/v1/openapi.json", openapi, name="openapi"),
]

s2s_urlpatterns = [
    path("_s2s/workspaces/ungrouped/", views.retrieve_ungrouped_workspace),
]

urlpatterns = [
    path("api/tenant/unmodified/", views.list_unmodified_tenants),
    path("api/tenant/", views.list_tenants),
    path("api/tenant/<str:org_id>/", views.tenant_view),
    path("api/roles/<str:uuid>/", views.roles),
    path("api/migrations/run/", views.run_migrations),
    path("api/migrations/progress/", views.migration_progress),
    path("api/seeds/run/", views.run_seeds),
    path("api/cars/expire/", views.car_expiry),
    path("api/cars/clean/", views.cars_clean),
    path("api/sentry_debug/", views.trigger_error),
    path("api/utils/sync_schemas/", views.sync_schemas),
    path("api/utils/set_tenant_ready/", views.set_tenant_ready),
    path("api/utils/populate_tenant_account_id/", views.populate_tenant_account_id),
    path("api/utils/populate_tenant_org_id/", views.populate_tenant_org_id_view),
    path("api/utils/invalid_default_admin_groups/", views.invalid_default_admin_groups),
    path("api/utils/ocm_performance/", views.ocm_performance),
    path("api/utils/get_org_admin/<int:org_or_account>/", views.get_org_admin),
    path("api/utils/role/", views.role_removal),
    path("api/utils/permission/", views.permission_removal),
    path("api/utils/username_lower/", views.username_lower),
    path("api/utils/data_migration/", views.data_migration),
    path("api/utils/bindings/<role_uuid>/", views.list_or_delete_bindings_for_role),
    path("api/utils/binding/<binding_id>/clean/", views.clean_binding_mapping),
    path("api/utils/bootstrap_tenant/", views.bootstrap_tenant),
    path("api/utils/bootstrap_pending_tenants/", views.bootstrap_pending_tenants),
    path("api/utils/migration_resources/", views.migration_resources),
    path("api/utils/fetch_replication_data/", views.fetch_replication_data),
    path("api/utils/reset_imported_tenants/", views.reset_imported_tenants),
    path("api/utils/resource_definitions/", views.correct_resource_definitions),
    path("api/utils/principal/", views.principal_removal),
    path("api/utils/user_lookup/", views.user_lookup),
    path("api/relations/lookup_resource/", views.lookup_resource),
    path("api/relations/lookup_subjects/", views.lookup_subjects),
    path("api/relations/check_relation/", views.check_relation),
    path("api/relations/read_tuples/", views.read_tuples),
    path("api/inventory/bootstrap_tenants/<org_id>/", views.check_bootstrapped_tenants),
    path("api/inventory/group_assignments/<group_uuid>/", views.group_assignments),
    path("api/inventory/check_workspace/<workspace_uuid>/", views.check_workspace_relation),
    path("api/inventory/check_role/<role_uuid>/", views.check_role),
    path("api/inventory/check_cross_account_request/<request_id>/", views.check_cross_account_request),
    path("api/inventory/check/", views.check_inventory),
    path("api/utils/kafka_test_message/", views.send_kafka_test_message),
    path("api/utils/migrate_binding_scope/", views.migrate_binding_scope),
    path("api/utils/fix_missing_binding_base_tuples/", views.fix_missing_binding_base_tuples),
    path(
        "api/utils/clean_invalid_workspace_resource_definitions/", views.clean_invalid_workspace_resource_definitions
    ),
    path("api/utils/cleanup_tenant_orphan_bindings/<str:org_id>/", views.cleanup_tenant_orphan_bindings),
    path("api/utils/bulk_cleanup_orphan_bindings/", views.bulk_cleanup_orphan_bindings),
    path("api/utils/rebuild_tenant_workspace_relations/<str:org_id>/", views.rebuild_tenant_workspace_relations),
    path("api/utils/remove_unassigned_system_binding_mappings/", views.remove_unassigned_system_binding_mappings),
    path("api/utils/expire_orphaned_cross_account_requests/", views.expire_orphaned_cross_account_requests),
    path("api/utils/remove_deleted_workspace_bindings/", views.remove_deleted_workspace_bindings),
    path(
        "api/utils/migrate_custom_v2_roles_to_tenant/",
        views.migrate_custom_v2_roles_to_tenant,
    ),
    path("api/utils/mcp_tool_descriptions/", views.mcp_tool_descriptions, name="mcp-tool-descriptions-list"),
    path(
        "api/utils/mcp_tool_descriptions/<str:tool_name>/",
        views.mcp_tool_descriptions,
        name="mcp-tool-descriptions-detail",
    ),
    path("api/utils/replicate_default_workspaces/", views.replicate_default_workspaces),
    path("api/utils/recompute_tenant_role_bindings/<str:org_id>/", views.recompute_tenant_role_bindings),
    path("api/disaster_recovery/workspaces/", views.recover_workspace_events),
    path("api/disaster_recovery/reconcile/", views.disaster_recovery_reconcile),
]

urlpatterns.extend(integration_urlpatterns)
urlpatterns.extend(s2s_urlpatterns)
