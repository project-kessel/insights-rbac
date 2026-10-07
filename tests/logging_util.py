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
"""Logging helpers for tests."""

import logging
from contextlib import contextmanager


@contextmanager
def enable_logging():
    """Re-enable logging temporarily so that assertLogs/assertNoLogs work.

    Django's parallel test runner calls logging.disable(CRITICAL), which suppresses the records
    assertLogs and assertNoLogs rely on. This restores logging within its scope and resets afterwards.
    """
    prior_disable = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    try:
        yield
    finally:
        logging.disable(prior_disable)
