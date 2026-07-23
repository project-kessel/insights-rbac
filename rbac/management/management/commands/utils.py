#
# Copyright 2019 Red Hat, Inc.
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
"""Functions for importing users data."""

import csv
import os

import boto3
from botocore.exceptions import ClientError
from django.db import IntegrityError, transaction
from management.principal.model import Principal
from management.role.relation_api_dual_write_handler import OutboxReplicator
from management.tenant_mapping.model import logger
from management.tenant_service.v2 import V2TenantBootstrapService
from management.workspace.model import Workspace

from api.models import Tenant, User

BOOT_STRAP_SERVICE = V2TenantBootstrapService(OutboxReplicator())


def get_file_path(file_name):
    """Get the file path for the users data."""
    return f"/tmp/{file_name}"


def get_s3_client():
    """Create and return an S3 client."""
    return boto3.client(
        "s3",
        aws_access_key_id=os.environ.get("S3_AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("S3_AWS_SECRET_ACCESS_KEY"),
    )


def download_data_from_S3(file_name):
    """Download users data from S3."""
    s3_client = get_s3_client()
    file_path = get_file_path(file_name)

    if os.path.exists(file_path):
        os.remove(file_path)
        logger.info(f"Removed existing data file: {file_path}")

    bucket = os.environ.get("S3_AWS_BUCKET")
    logger.info(f"Downloading file from S3 bucket: {bucket}")
    try:
        s3_client.download_file(f"{bucket}", f"{file_name}", file_path)
        logger.info("File downloaded successfully.")
    except ClientError as e:
        logger.info(f"Error downloading file: {e}")
        raise


def populate_tenant_user_data(file_name, start_line=1, batch_size=1000):
    """
    Populate tenant and user data from the downloaded file.

    Args:
        batch_size (int): Number of records to process in each batch.
        start_line(int): Line number to start processing from (1).
    """
    file_path = get_file_path(file_name)
    with open(file_path, "r") as file:
        csv_reader = csv.reader(file)
        # Skip lines until start
        for _ in range(start_line):
            next(csv_reader, None)
        batch_data = []
        for current_line, row in enumerate(csv_reader, start=start_line):
            org_id, admin, principal_name, user_id = row
            admin = admin == "admin:org:all"
            batch_data.append((org_id, admin, principal_name, user_id))
            if len(batch_data) >= batch_size:
                process_batch(batch_data)
                logger.info(f"Processed batch ending at line {current_line}")
                batch_data = []
        # Process any remaining records
        if batch_data:
            process_batch(batch_data)
            logger.info(f"Processed final batch ending at line {current_line}")
    return


def process_batch(batch_data):
    """Process a batch of tenant and principal data."""
    users = []
    for org_id, org_admin, principal_name, user_id in batch_data:
        user = User()
        user.org_id = org_id
        user.admin = org_admin
        user.username = principal_name
        user.user_id = user_id
        user.is_active = True
        users.append(user)
    try:
        # In atomic block so that if anything goes wrong, the whole batch is rolled back and can be retried
        # Otherwise we may have some partial tenant data, and a tenant may not get fully bootstrapped.
        with transaction.atomic():
            BOOT_STRAP_SERVICE.import_bulk_users(users)
    except IntegrityError as e:
        """Retry once if there is creation conflict."""
        logger.info(f"IntegrityError: {e.__cause__}. Retrying import.")
        with transaction.atomic():
            BOOT_STRAP_SERVICE.import_bulk_users(users)


def populate_service_account_data(file_name):
    """Populate service account data from the downloaded file."""
    file_path = get_file_path(file_name)
    id_mapping = {}
    with open(file_path, "r") as file:
        csv_reader = csv.reader(file)
        next(csv_reader, None)  # Skip header
        for row in csv_reader:
            user_id, client_id = row
            id_mapping[client_id] = user_id
    for principal in Principal.objects.filter(type=Principal.Types.SERVICE_ACCOUNT):
        if user_id := id_mapping.get(principal.service_account_id):
            principal.user_id = user_id
            principal.save()


def populate_workspace_data(file_name, batch_size=250):
    """Populate workspace data from the downloaded file."""
    file_path = get_file_path(file_name)
    current_line = 0
    with open(file_path, "r") as file:
        csv_reader = csv.DictReader(file)
        batch_data = []
        for row in csv_reader:
            current_line += 1
            batch_data.append(row)
            if len(batch_data) >= batch_size:
                batch_import_workspace(batch_data)
                logger.info(f"Processed batch ending at line {current_line}")
                batch_data = []
        # Process any remaining records
        if batch_data:
            batch_import_workspace(batch_data)
            logger.info(f"Processed final batch ending at line {current_line}")
    return


def batch_import_workspace(records):
    """Import workspace records in batch."""
    with transaction.atomic():
        workspaces = []
        workspaces_to_update = []
        pairs = []
        tenants = Tenant.objects.filter(org_id__in=[record["org_id"] for record in records])
        tenant_dict = {tenant.org_id: tenant for tenant in tenants}
        workspace_ids = [record["id"] for record in records]
        existing_wss = Workspace.objects.filter(id__in=workspace_ids)
        existing_wss_dict = {str(existing_ws.id): existing_ws for existing_ws in existing_wss}
        parent_workspaces = Workspace.objects.filter(tenant__in=tenants, type=Workspace.Types.DEFAULT).select_related(
            "tenant"
        )
        parent_workspace_dict = {}
        for workspace in parent_workspaces:
            parent_workspace_dict[workspace.tenant.org_id] = workspace

        for record in records:
            is_ungrouped = record["ungrouped"].lower() == "true"
            parent = parent_workspace_dict.get(record["org_id"])
            if not parent:
                logger.warning(f"Missing tenant for org_id: {record['org_id']}")
                bootstrapped_tenant = BOOT_STRAP_SERVICE._get_or_bootstrap_tenant(record["org_id"], ready=True)
                tenant_dict[record["org_id"]] = bootstrapped_tenant.tenant
                parent = Workspace.objects.default(tenant=bootstrapped_tenant.tenant)
            if is_ungrouped:
                ws_name = Workspace.SpecialNames.UNGROUPED_HOSTS
                workspace_type = Workspace.Types.UNGROUPED_HOSTS
            else:
                ws_name = record["name"]
                workspace_type = Workspace.Types.STANDARD

            if record["id"] in existing_wss_dict:
                workspace = existing_wss_dict[record["id"]]
                workspace.name = ws_name
                workspace.modified = record["modified_on"]
                workspaces_to_update.append(workspace)
            else:
                workspace = Workspace(
                    id=record["id"],
                    name=ws_name,
                    tenant=tenant_dict[record["org_id"]],
                    type=workspace_type,
                    parent=parent,
                    created=record["created_on"],
                    modified=record["modified_on"],
                )
                workspaces.append(workspace)
            pairs.append((str(workspace.id), str(parent.id)))

        Workspace.objects.bulk_create(workspaces)
        Workspace.objects.bulk_update(workspaces_to_update, ["name", "modified"])
        BOOT_STRAP_SERVICE.create_workspace_relationships(pairs)
