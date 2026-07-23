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
"""Tests for AtomicOperationsMixin."""

from django.test import SimpleTestCase

from management.v2_mixins import AtomicOperationsMixin


class AtomicOperationsMixinGuardTests(SimpleTestCase):
    """Verify that the __init_subclass__ guard prevents overriding guarded methods."""

    def test_override_create_raises(self):
        with self.assertRaises(TypeError) as ctx:

            class Bad(AtomicOperationsMixin):
                def create(self, request, *args, **kwargs): ...

        self.assertIn("create", str(ctx.exception))
        self.assertIn("perform_atomic_create", str(ctx.exception))

    def test_override_update_raises(self):
        with self.assertRaises(TypeError) as ctx:

            class Bad(AtomicOperationsMixin):
                def update(self, request, *args, **kwargs): ...

        self.assertIn("update", str(ctx.exception))
        self.assertIn("perform_atomic_update", str(ctx.exception))

    def test_override_destroy_raises(self):
        with self.assertRaises(TypeError) as ctx:

            class Bad(AtomicOperationsMixin):
                def destroy(self, request, *args, **kwargs): ...

        self.assertIn("destroy", str(ctx.exception))
        self.assertIn("perform_atomic_destroy", str(ctx.exception))

    def test_override_perform_atomic_create_allowed(self):
        class Good(AtomicOperationsMixin):
            def perform_atomic_create(self, request, *args, **kwargs): ...

    def test_override_perform_atomic_update_allowed(self):
        class Good(AtomicOperationsMixin):
            def perform_atomic_update(self, request, *args, **kwargs): ...

    def test_override_perform_atomic_destroy_allowed(self):
        class Good(AtomicOperationsMixin):
            def perform_atomic_destroy(self, request, *args, **kwargs): ...

    def test_unrelated_methods_allowed(self):
        class Good(AtomicOperationsMixin):
            def bulk_destroy(self, request, *args, **kwargs): ...

            def batch_create(self, request, *args, **kwargs): ...
