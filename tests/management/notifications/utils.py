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
"""Helpers for testing RH system notifications."""

from contextlib import contextmanager

from management.notifications.notification_handlers import rh_notification_collector


@contextmanager
def collected_rh_events(test_case):
    """Yield the RH events committed inside the block, as the seeds command would collect them."""
    events = []
    token = rh_notification_collector.set(events)
    try:
        with test_case.captureOnCommitCallbacks(execute=True):
            yield events
    finally:
        rh_notification_collector.reset(token)
