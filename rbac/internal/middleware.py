#
# Copyright 2020 Red Hat, Inc.
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

"""Custom Internal RBAC Middleware."""

import binascii
import logging
from json.decoder import JSONDecodeError

from django.conf import settings
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404
from django.urls import resolve
from django.utils.deprecation import MiddlewareMixin
from management.authorization.token_validator import ITSSOTokenValidator, TokenValidator
from management.utils import build_system_user_from_token, build_user_from_psk

from api.common import RH_IDENTITY_HEADER
from api.models import Tenant
from api.serializers import extract_header
from rbac.a2s import is_a2s_path
from .utils import build_internal_user

logger = logging.getLogger(__name__)


class InternalIdentityHeaderMiddleware(MiddlewareMixin):
    """Middleware for the internal identity header."""

    header = RH_IDENTITY_HEADER
    token_validator: TokenValidator = ITSSOTokenValidator()

    def process_request(self, request):
        """Process request for internal identity middleware."""
        if not any([request.path.startswith(prefix) for prefix in settings.INTERNAL_API_PATH_PREFIXES]):
            # We are not in an internal API section
            return

        if is_a2s_path(request):
            # A2S (agent-to-service) paths use public IdentityHeaderMiddleware auth
            return

        user = None
        # If the path starts with /_private/_s2s/, it is using psk to authenticate
        if request.path.startswith("/_private/_s2s/"):
            req_id = getattr(request, "req_id", None)
            logger.info(
                "S2S request received, attempting authentication [request_id=%s, method=%s, path=%s]",
                req_id,
                request.method,
                request.path,
            )
            user = build_user_from_psk(request) or build_system_user_from_token(request, self.token_validator)

            if not user:
                logger.error(
                    "S2S auth failed: no identity (PSK/token failed) [request_id=%s, method=%s, path=%s]",
                    req_id,
                    request.method,
                    request.path,
                )
                return HttpResponseForbidden()
        else:
            try:
                _, json_rh_auth = extract_header(request, self.header)
            except (JSONDecodeError, binascii.Error, KeyError):
                logger.exception("Invalid X-RH-Identity header.")
                return HttpResponseForbidden()
            user = build_internal_user(request, json_rh_auth)
            if not user:
                logger.error("Malformed X-RH-Identity header.")
                return HttpResponseForbidden()
            try:
                path_org_id = resolve(request.path).kwargs.get("org_id")
                if path_org_id:
                    request.tenant = get_object_or_404(Tenant, org_id=user.org_id)
            except (KeyError, TypeError):
                logger.error("Malformed X-RH-Identity header.")
                return HttpResponseForbidden()

        request.user = user

    def process_response(self, request, response):
        """Process responses for internal identity middleware."""
        return response
