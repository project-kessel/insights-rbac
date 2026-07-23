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
from unittest import TestCase

from migration_tool.in_memory_tuples import InMemoryTuples
from .v2_local_invariants import assert_v1_v2_locally_consistent as assert_v1_v2_locally_consistent
from .v2_tuple_invariants import assert_v2_tuples_consistent as assert_v2_tuples_consistent


def assert_v1_v2_tuples_fully_consistent(test: TestCase, tuples: InMemoryTuples):
    assert_v1_v2_locally_consistent(test=test)
    assert_v2_tuples_consistent(test=test, tuples=tuples)
