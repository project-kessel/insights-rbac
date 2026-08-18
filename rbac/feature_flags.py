#
# Copyright 2025 Red Hat, Inc.
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
"""Feature flag module module."""

import logging
import threading
from typing import Callable, Optional

from UnleashClient import UnleashClient
from django.conf import settings

logger = logging.getLogger(__name__)


class FeatureFlags:
    """Feature flag class."""

    # Add ungrouped hosts' IDs to the returning payloads.
    TOGGLE_ADD_UNGROUPED_HOSTS_ID = "rbac.access-add-ungrouped-hosts-id.enabled"
    # Removes the null value from the list of workspace IDs.
    TOGGLE_REMOVE_NULL_VALUE = "rbac.resource-definition-remove-null-value.enabled"
    # Makes the V2 API to only allow "GET" requests.
    TOGGLE_V2_API_READONLY = "rbac.v2-api-readonly-mode.enabled"
    # Enable read-your-writes wait for workspace operations
    TOGGLE_READ_YOUR_WRITES_WORKSPACE = "rbac.read-your-writes.workspace.enabled"
    # Enables Inventory API access check v2 for workspace permissions.
    TOGGLE_WORKSPACE_ACCESS_CHECK_V2 = "rbac.workspace-access-check-v2.enabled"
    # When enabled, use 'role_binding_view' permission; when disabled, use 'view' permission for role binding access.
    TOGGLE_USE_ROLE_BINDING_VIEW_PERMISSION = "rbac.use-role-binding-view-permission.enabled"
    # Per-org flag: when enabled, the org uses v2 APIs for write operations and v1 write APIs are blocked.
    TOGGLE_V2_EDIT_API_ENABLED = "platform.rbac.workspaces"
    # When enabled, use Kafka for principal cleanup; when disabled, use UMB.
    TOGGLE_USE_KAFKA_CLEANUP = "rbac.principal-cleanup.use-kafka.enabled"
    # Per-org flag: when enabled, the org uses only V2 access checks for HBI (and V1 access checks are blocked).
    TOGGLE_V2_ADDITIONAL_MANDATORY_ACCESS_CHECK_REQUIRED = "hbi.rbac-v2"

    def __init__(self):
        """Add attributes."""
        self.client = None
        self._lock = threading.Lock()

    def initialize(self):
        """Set the client on an instance with thread safety."""
        if self.client is not None:
            return

        # Acquire a lock and double-check to avoid race conditions
        with self._lock:
            if self.client is not None:
                return

            try:
                self.client = self._init_unleash_client()
                logger.info("Feature flags client initialized successfully.")
            except Exception:
                logger.exception("Error initializing FeatureFlags client")
                self.client = None

    def _init_unleash_client(self):
        """Initialize the client."""
        client = UnleashClient(
            url=settings.FEATURE_FLAGS_URL,
            app_name=settings.APP_NAME,
            custom_headers={"Authorization": settings.FEATURE_FLAGS_TOKEN},
            cache_directory=settings.FEATURE_FLAGS_CACHE_DIR,
        )

        if settings.FEATURE_FLAGS_URL and settings.FEATURE_FLAGS_TOKEN:
            client.initialize_client()
            logger.info(f"FeatureFlags initialized using Unleash on {settings.FEATURE_FLAGS_URL}")
        else:
            logger.info(
                "FEATURE_FLAGS_URL and/or FEATURE_FLAGS_TOKEN were not set, skipping FeatureFlags initialization."
            )

        return client

    def is_enabled(
        self,
        feature_name: str,
        context: Optional[dict] = None,
        fallback_function: Optional[Callable[[str, Optional[dict]], bool]] = None,
    ):
        """Override of is_enabled for checking flag values."""
        if self.client is None:
            self.initialize()

        if self.client is None:
            if fallback_function:
                logger.warning("FeatureFlags not initialized, using fallback function")
                return fallback_function(feature_name, context)
            else:
                logger.warning("FeatureFlags not initialized, defaulting to False")
                return False

        return self.client.is_enabled(feature_name, context, fallback_function=fallback_function)

    def is_add_ungrouped_hosts_id_enabled(self):
        """
        Check if "add ungrouped hosts ID" feature is enabled.

        Falls back to reading the environment variable if any error occurs.
        """
        return self.is_enabled(
            feature_name=self.TOGGLE_ADD_UNGROUPED_HOSTS_ID,
            fallback_function=lambda ignored_toggle_name, ignored_context: settings.ADD_UNGROUPED_HOSTS_ID,
        )

    def is_remove_null_value_enabled(self):
        """Check whether the "remove null value" feature is enabled.

        Falls back to reading the environment variable if any error occurs.
        """
        return self.is_enabled(
            feature_name=self.TOGGLE_REMOVE_NULL_VALUE,
            fallback_function=lambda ignored_toggle_name, ignored_context: settings.REMOVE_NULL_VALUE,
        )

    def is_v2_api_read_only_mode_enabled(self):
        """Check whether the "v2 API in readonly mode" feature is enabled.

        Falls back to reading the environment variable if any error occurs.
        """
        return self.is_enabled(
            feature_name=self.TOGGLE_V2_API_READONLY,
            fallback_function=lambda ignored_toggle_name, ignored_context: settings.V2_READ_ONLY_API_MODE,
        )

    def is_read_your_writes_workspace_enabled(self):
        """Check whether read-your-writes for workspaces is enabled.

        Falls back to reading the environment variable if any error occurs.
        """
        return self.is_enabled(
            feature_name=self.TOGGLE_READ_YOUR_WRITES_WORKSPACE,
            fallback_function=lambda ignored_toggle_name, ignored_context: settings.READ_YOUR_WRITES_WORKSPACE_ENABLED,
        )

    def is_workspace_access_check_v2_enabled(self):
        """Check whether the "workspace access check v2" feature is enabled.

        Falls back to reading the environment variable if any error occurs.
        """
        return self.is_enabled(
            feature_name=self.TOGGLE_WORKSPACE_ACCESS_CHECK_V2,
            fallback_function=lambda ignored_toggle_name, ignored_context: settings.WORKSPACE_ACCESS_CHECK_V2_ENABLED,
        )

    def is_use_role_binding_view_permission_enabled(self):
        """Check whether to use 'role_binding_view' permission for role binding access.

        When enabled (True), use 'role_binding_view' permission.
        When disabled (False), use 'view' permission.
        Falls back to reading the environment variable if any error occurs.
        """
        return self.is_enabled(
            feature_name=self.TOGGLE_USE_ROLE_BINDING_VIEW_PERMISSION,
            fallback_function=lambda ignored_toggle_name, ignored_context: settings.USE_ROLE_BINDING_VIEW_PERMISSION,
        )

    def is_v2_edit_api_enabled(self, org_id: str) -> bool:
        """Check whether v2 write APIs are enabled for the given org.

        When enabled, the org should use v2 APIs and v1 write operations are blocked.
        When disabled, the org should use v1 APIs and v2 write operations are blocked.

        Uses orgId in context to match Unleash strategy constraints (contextName: orgId).
        """
        return self.is_enabled(
            feature_name=self.TOGGLE_V2_EDIT_API_ENABLED,
            context={"orgId": str(org_id)},
            fallback_function=lambda ignored_toggle_name, ignored_context: settings.V2_EDIT_API_ENABLED,
        )

    def is_kafka_principal_cleanup_enabled(self):
        """Check whether to use Kafka for principal cleanup.

        DEPRECATED: Use get_principal_cleanup_mode() instead for 3-state control.

        When enabled (True), use Kafka for principal cleanup.
        When disabled (False), use UMB for principal cleanup.
        Falls back to False (UMB) if Unleash is unavailable - UMB is the proven original method.
        """
        return self.is_enabled(
            feature_name=self.TOGGLE_USE_KAFKA_CLEANUP,
            fallback_function=lambda ignored_toggle_name, ignored_context: False,
        )

    def get_principal_cleanup_mode(self) -> str:
        """
        Get the principal cleanup mode using the feature flag.

        This method supports 4 modes controlled by the Unleash flag value:
        - 'umb_only' (flag disabled OR flag enabled with missing/unrecognized variant):
          Only UMB consumer runs and writes to DB
        - 'kafka_shadow' (flag enabled with kafka_shadow variant): Both UMB and Kafka run, only UMB writes
          (Kafka dry-run)
        - 'kafka_validation' (flag enabled with kafka_validation variant): Kafka tries to write first,
          UMB writes only if Kafka fails (Phase 2 validation)
        - 'kafka_active' (flag enabled with kafka_active variant): Only Kafka consumer runs and writes to DB

        IMPORTANT: Enabling the flag WITHOUT setting a valid variant will keep you on UMB (umb_only mode).
        This is a safety feature to prevent accidental Kafka activation. You must explicitly set the variant
        to kafka_shadow, kafka_validation, or kafka_active to use Kafka.

        Returns:
            str: One of 'umb_only', 'kafka_shadow', 'kafka_validation', or 'kafka_active'
        """
        if self.client is None:
            self.initialize()

        if self.client is None:
            logger.warning("FeatureFlags not initialized, defaulting to umb_only mode")
            return "umb_only"

        # First check if the toggle is enabled
        is_enabled = self.is_enabled(
            feature_name=self.TOGGLE_USE_KAFKA_CLEANUP,
            fallback_function=lambda ignored_toggle_name, ignored_context: False,
        )

        if not is_enabled:
            # Flag is disabled -> UMB only mode
            return "umb_only"

        # Flag is enabled -> check for variant to determine mode
        try:
            variant = self.client.get_variant(
                self.TOGGLE_USE_KAFKA_CLEANUP,
                fallback_variant={"name": "umb_only", "enabled": False},
            )

            mode = variant.get("name", "umb_only")

            # Validate mode - only these variants are valid when flag is enabled
            if mode in ["kafka_shadow", "kafka_validation", "kafka_active"]:
                return mode
            else:
                # Unknown/missing variant when flag is enabled -> safe default is UMB
                # This prevents accidental Kafka activation and requires explicit variant selection
                logger.warning(
                    f"Flag 'rbac.principal-cleanup.use-kafka.enabled' is ON but variant '{mode}' is not recognized. "
                    f"Staying on UMB (umb_only mode) for safety. "
                    f"To activate Kafka, set variant to: kafka_shadow, kafka_validation, or kafka_active"
                )
                return "umb_only"

        except Exception as e:
            logger.warning(f"Error getting variant for principal cleanup mode: {e}, falling back to umb_only")
            return "umb_only"

    def is_kafka_shadow_mode_enabled(self) -> bool:
        """Check if Kafka is in shadow/dry-run mode."""
        return self.get_principal_cleanup_mode() == "kafka_shadow"

    def is_v2_strict_access_check_enabled(self, org_id: str) -> bool:
        """Check whether strict V2 access checks are required in the given org.

        When enabled, apps that have opted-in to strict V2 access checks must only use V2 access checks for the org,
        and V1 access checks are blocked (even if the org has not yet migrated to V2).

        It is expected that this is set for a superset of the orgs that TOGGLE_V2_EDIT_API_ENABLED is set for.
        """
        # Note that we use the same fallback_function as is_v2_edit_api_enabled above in order to maintain the
        # invariant that this flag is set for a superset of the other flag.
        return self.is_enabled(
            feature_name=self.TOGGLE_V2_ADDITIONAL_MANDATORY_ACCESS_CHECK_REQUIRED,
            context={"orgId": str(org_id)},
            fallback_function=lambda ignored_toggle_name, ignored_context: settings.V2_EDIT_API_ENABLED,
        )


FEATURE_FLAGS = FeatureFlags()
