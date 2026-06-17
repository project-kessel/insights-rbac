"""Tests for remove_legacy_root_workspace_tenant_parent_relations management command."""

#
# Copyright 2026 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
from unittest.mock import ANY, patch

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings


class RemoveLegacyRootWorkspaceTenantParentRelationsCommandTests(TestCase):
    """Tests for remove_legacy_root_workspace_tenant_parent_relations."""

    def test_requires_all(self):
        with self.assertRaises(CommandError):
            call_command("remove_legacy_root_workspace_tenant_parent_relations")

    @override_settings(REPLICATION_TO_RELATION_ENABLED=False)
    @patch("management.management.commands.remove_legacy_root_workspace_tenant_parent_relations.logger")
    def test_skipped_logs_reason(self, mock_logger):
        call_command(
            "remove_legacy_root_workspace_tenant_parent_relations",
            "--all",
        )
        mock_logger.warning.assert_called_once_with("Skipped: %s", ANY)
        _, reason = mock_logger.warning.call_args[0]
        self.assertIn("REPLICATION_TO_RELATION_ENABLED", reason)

    @patch(
        "management.management.commands.remove_legacy_root_workspace_tenant_parent_relations."
        "remove_legacy_root_workspace_tenant_parent_relations"
    )
    @patch("management.management.commands.remove_legacy_root_workspace_tenant_parent_relations.logger")
    def test_all_invokes_util(self, mock_logger, mock_remove):
        mock_remove.return_value = {
            "skipped": False,
            "tenants_processed": 2,
        }
        call_command("remove_legacy_root_workspace_tenant_parent_relations", "--all")
        mock_remove.assert_called_once_with()
        mock_logger.info.assert_any_call("Processed %s tenants", 2)
