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

from contextlib import contextmanager, nullcontext

from management.notifications.model import RhNotificationEvent


@contextmanager
def collected_rh_events(test_case=None):
    """
    Yield a list that is filled with the RH events queued inside the block, in queue order, once the block exits.

    With a test case, on-commit callbacks registered in the block run as well, as they would after a real commit.
    """
    existing = set(RhNotificationEvent.objects.values_list("id", flat=True))
    events = []
    on_commit = test_case.captureOnCommitCallbacks(execute=True) if test_case else nullcontext()
    with on_commit:
        yield events
    events.extend(
        RhNotificationEvent.objects.exclude(id__in=existing).order_by("created", "id").values("event_type", "payload")
    )
