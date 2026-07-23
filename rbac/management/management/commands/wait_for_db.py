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
"""Custom management command to wait for the database to be available."""

import time

from django.core.management.base import BaseCommand
from django.db import OperationalError, connections


class Command(BaseCommand):
    """Custom command to wait for the database to be available."""

    def handle(self, *args, **kwargs):
        """Handle the command execution."""
        self.stdout.write("Waiting for the database connection...")
        retries = 5  # Number of retries
        wait_time = 2
        while retries > 0:
            try:
                connections["default"].ensure_connection()
                self.stdout.write(self.style.SUCCESS("Database connection established."))
                return
            except OperationalError as e:
                self.stdout.write(f"Database not available: {e}. Retrying in 1 second...")
                retries -= 1
                time.sleep(wait_time)
                wait_time *= 2  # Exponential backoff

        self.stderr.write(self.style.ERROR("Database connection failed after retries."))
        raise OperationalError("Unable to connect to the database.")
