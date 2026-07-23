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
"""API models for import organization."""

import binascii
import logging
from base64 import b64decode
from json import loads as json_loads

from django.utils.translation import gettext as _

from api.status.serializer import StatusSerializer  # noqa: F401

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


def error_obj(key, message):
    """Create an error object."""
    error = {key: [_(message)]}
    return error


def add_padding(encoded_header):
    """
    Calculate and add padding to a Base64 encoded header string.

    Args:
        encoded_header (str): The Base64 encoded header without padding.

    Returns:
        str: The Base64 encoded header with padding added to make its length a multiple of 4.
    """
    return encoded_header + "=" * (-len(encoded_header) % 4)


def extract_header(request, header):
    """Extract and decode json header.

    Args:
        request(object): The incoming request
        header(str): The header to decode
    Returns:
        Encoded(str): Base64 header
        Decoded(dict): Identity dictionary
    """
    rh_auth_header = request.META[header]
    try:
        decoded_rh_auth = b64decode(rh_auth_header)
    except binascii.Error as err:
        logger.warning("Could not decode header: %s.", err)
        logger.warning(rh_auth_header)
        logger.warning("Trying adding padding to header for decode ...")
        rh_auth_header = add_padding(rh_auth_header)
        decoded_rh_auth = b64decode(rh_auth_header)
    json_rh_auth = json_loads(decoded_rh_auth)
    return rh_auth_header, json_rh_auth


def create_tenant_name(account):
    """Create a database tenant name."""
    return f"acct{account}"
