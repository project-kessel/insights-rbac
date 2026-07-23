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
"""Celery tasks."""

from __future__ import absolute_import, unicode_literals

from celery import shared_task

from api.cross_access.util import check_cross_request_expiry
from api.utils import migration_resource_deletion, populate_tenant_account_id, reset_imported_tenants


@shared_task
def cross_account_cleanup():
    """Celery task to mark expired cross-account requests."""
    check_cross_request_expiry()


@shared_task
def populate_tenant_account_id_in_worker():
    """Celery task to populate Tenant#account_id."""
    populate_tenant_account_id()


@shared_task
def run_migration_resource_deletion(kwargs):
    """Celery task to run migration resource deletion."""
    migration_resource_deletion(**kwargs)


@shared_task
def run_reset_imported_tenants(kwargs):
    """Celery task to run reset imported tenants."""
    reset_imported_tenants(**kwargs)
