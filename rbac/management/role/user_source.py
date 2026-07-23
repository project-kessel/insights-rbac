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
"""Provides the SourceKey class for use with BindingMapping."""


class SourceKey:
    """
    Identifier of a source for a principal bound to a BindingMapping.

    For instance, this might identify an active CrossAccountRequest that's resulting in a user being bound to a
    BindingMapping.
    """

    key: str

    def __init__(self, source, source_id: str):
        """
        Initialize a SourceKey.

        Params:
        * source: the model causing the user to be bound to the BindingMapping
        * source_id: an identifier for the model, unique among any given type of model source
        """
        self.key = f"{source.__class__.__name__}/{source_id}"

    def __hash__(self):
        """Hash value for the SourceKey instance."""
        return hash(self.key)

    def __str__(self):
        """Return the string representation of the SourceKey instance."""
        return f"{self.key}"
