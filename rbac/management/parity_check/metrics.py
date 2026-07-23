#
# Copyright 2026 Red Hat, Inc.
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
"""Prometheus metrics for parity access checks."""

from prometheus_client import Counter, Histogram

# Counter for total parity checks performed
parity_checks_total = Counter(
    "rbac_parity_checks_total",
    "Total number of parity access checks performed",
    ["check_type", "result"],
)

# Counter for parity discrepancies found
parity_discrepancies_total = Counter(
    "rbac_parity_discrepancies_total",
    "Total number of parity discrepancies found between RBAC and PDP",
    ["discrepancy_type"],
)

# Histogram for parity check duration
parity_check_duration_seconds = Histogram(
    "rbac_parity_check_duration_seconds",
    "Time spent performing parity checks",
    ["check_type"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

# Counter for tenants checked
parity_tenants_checked_total = Counter(
    "rbac_parity_tenants_checked_total",
    "Total number of tenants checked for parity",
    ["status"],
)

# Counter for principals checked
parity_principals_checked_total = Counter(
    "rbac_parity_principals_checked_total",
    "Total number of principals checked for parity",
    ["status"],
)

# Counter for job runs
parity_job_runs_total = Counter(
    "rbac_parity_job_runs_total",
    "Total number of parity check job runs",
    ["status"],
)

# Histogram for workspace access check duration
parity_workspace_check_duration_seconds = Histogram(
    "rbac_parity_workspace_check_duration_seconds",
    "Time spent checking workspace access parity for a single principal",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
