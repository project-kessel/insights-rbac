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
"""Test the API serializers module."""

from unittest.mock import Mock

from django.test import TestCase

from api.serializers import extract_header

BAD_PADDING_HEADER = "eyJpZGVudGl0eSI6eyJhY2NvdW50X251bWJlciI6IjYwODk3MTkiLCJ0eXBlIjoiVXNlciIsInVzZXIiOnsidXNlcm5hbWUiOiJzeXNhZG1pbiIsImVtYWlsIjoic3lzYWRtaW4iLCJmaXJzdF9uYW1lIjoic3lzYWRtaW4iLCJsYXN0X25hbWUiOiJzeXNhZG1pbiIsImlzX2FjdGl2ZSI6dHJ1ZSwiaXNfb3JnX2FkbWluIjp0cnVlLCJpc19pbnRlcm5hbCI6ZmFsc2UsImxvY2FsZSI6ImVuX1VTIn0sImludGVybmFsIjp7Im9yZ19pZCI6IjExNzg5NzcyIiwiYXV0aF90eXBlIjoiand0LWF1dGgiLCJhdXRoX3RpbWUiOjB9fSwidXNlciI6eyJ1c2VybmFtZSI6InN5c2FkbWluIiwiZW1haWwiOiJzeXNhZG1pbiIsImZpcnN0X25hbWUiOiJzeXNhZG1pbiIsImxhc3RfbmFtZSI6InN5c2FkbWluIiwiaXNfYWN0aXZlIjp0cnVlLCJpc19vcmdfYWRtaW4iOnRydWUsImlzX2ludGVybmFsIjpmYWxzZSwibG9jYWxlIjoiZW5fVVMifX0"


class SerializersTest(TestCase):
    """Tests against the serializer functions."""

    def test_handle_bad_padding(self):
        """Test the handling of bad padding."""
        request = Mock()
        request.META = {"header1": BAD_PADDING_HEADER}
        try:
            extract_header(request, "header1")
        except Exception:
            self.fail("Should handle padding error.")
