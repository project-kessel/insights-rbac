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

"""Common exception handler class."""

import copy

from django.db import IntegrityError
from django.http import Http404
from management.authorization.invalid_token import InvalidTokenError
from management.authorization.missing_authorization import MissingAuthorizationError
from management.authorization.unable_meet_prerequisites import UnableMeetPrerequisitesError
from management.exceptions import InvalidFieldError, NotFoundError, RequiredFieldError
from management.role.v2_exceptions import RolesNotFoundError
from management.utils import api_path_prefix, v2response_error_from_errors
from rest_framework import status
from rest_framework.views import Response, exception_handler


def _generate_errors_from_list(data, **kwargs):
    """Create error objects based on the exception."""
    errors = []
    status_code = kwargs.get("status_code", 0)
    source = kwargs.get("source")
    for value in data:
        if isinstance(value, str):
            new_error = {"detail": value, "source": source, "status": status_code}
            errors.append(new_error)
        elif isinstance(value, list):
            errors += _generate_errors_from_list(value, **kwargs)
        elif isinstance(value, dict):
            errors += _generate_errors_from_dict(value, **kwargs)
    return errors


def _generate_errors_from_dict(data, **kwargs):
    """Create error objects based on the exception."""
    errors = []
    status_code = kwargs.get("status_code", 0)
    source = kwargs.get("source")
    for key, value in data.items():
        source_val = "{}.{}".format(source, key) if source else key
        if isinstance(value, str):
            new_error = {"detail": value, "source": source_val, "status": status_code}
            errors.append(new_error)
        elif isinstance(value, list):
            kwargs["source"] = source_val
            errors += _generate_errors_from_list(value, **kwargs)
        elif isinstance(value, dict):
            kwargs["source"] = source_val
            errors += _generate_errors_from_dict(value, **kwargs)
    return errors


def _v2_generate_error_data_payload_response(detail: str, context, http_status_code: int) -> dict:
    """Generate the payload for the "data" parameter of the response."""
    data = _generate_error_data_payload_response(detail=detail, context=context, http_status_code=http_status_code)
    return v2response_error_from_errors(data["errors"])


def _generate_error_data_payload_response(detail: str, context, http_status_code: int) -> dict:
    """Generate the payload for the "data" parameter of the response."""
    data = {
        "errors": [
            {
                "detail": detail,
                "status": str(http_status_code),
            }
        ]
    }

    # Some exceptions might be raised from places that are not views.
    view = context.get("view")
    if view and hasattr(view, "basename"):
        data["errors"][0]["source"] = view.basename

    return data


def custom_exception_handler_v2(exc, context):
    """Create custom response for v2 exceptions."""
    response = exception_handler(exc, context)

    # Now add the HTTP status code to the response.
    if response is not None:
        response.content_type = "application/problem+json"
        errors = []
        data = copy.deepcopy(response.data)
        if isinstance(data, dict):
            errors += _generate_errors_from_dict(data, **{"status_code": str(response.status_code)})
        elif isinstance(data, list):
            errors += _generate_errors_from_list(data, **{"status_code": str(response.status_code)})
        error_response = v2response_error_from_errors(errors=errors, exc=exc, context=context)
        response.data = error_response
    elif isinstance(exc, IntegrityError):
        source_view = context.get("view")
        errors = [{"detail": str(exc), "source": f"{source_view.basename}", "status": "400"}]
        response = Response(
            data=v2response_error_from_errors(errors=errors, exc=exc, context=context),
            content_type="application/problem+json",
            status=status.HTTP_400_BAD_REQUEST,
        )
    elif isinstance(exc, InvalidTokenError):
        response = Response(
            data=_v2_generate_error_data_payload_response(
                detail="Invalid token provided.", context=context, http_status_code=status.HTTP_401_UNAUTHORIZED
            ),
            content_type="application/json",
            status=status.HTTP_401_UNAUTHORIZED,
        )
    elif isinstance(exc, MissingAuthorizationError):
        response = Response(
            data=_v2_generate_error_data_payload_response(
                detail="A Bearer token in an authorization header is required when performing service account"
                " operations.",
                context=context,
                http_status_code=status.HTTP_401_UNAUTHORIZED,
            ),
            content_type="application/json",
            status=status.HTTP_401_UNAUTHORIZED,
        )
    elif isinstance(exc, UnableMeetPrerequisitesError):
        response = Response(
            data=_v2_generate_error_data_payload_response(
                detail="Unable to validate the provided token.",
                context=context,
                http_status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            ),
            content_type="application/json",
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    elif isinstance(exc, RolesNotFoundError):
        # Convert RolesNotFoundError to Http404 and let standard handler process it
        response = exception_handler(Http404(str(exc)), context)
        if response is not None:
            response.content_type = "application/problem+json"
            errors = _generate_errors_from_dict(response.data, **{"status_code": str(response.status_code)})
            response.data = v2response_error_from_errors(errors=errors, exc=exc, context=context)
    elif isinstance(exc, NotFoundError):
        response = exception_handler(Http404(str(exc)), context)
        if response is not None:
            response.content_type = "application/problem+json"
            errors = _generate_errors_from_dict(response.data, **{"status_code": str(response.status_code)})
            response.data = v2response_error_from_errors(errors=errors, exc=exc, context=context)
    elif isinstance(exc, (InvalidFieldError, RequiredFieldError)):
        field = getattr(exc, "field", None) or getattr(exc, "field_name", None)
        errors = [{"detail": str(exc), "source": field, "status": "400"}]
        response = Response(
            data=v2response_error_from_errors(errors=errors, exc=exc, context=context),
            content_type="application/problem+json",
            status=status.HTTP_400_BAD_REQUEST,
        )

    return response


def exception_version_handler(exc, context):
    """Select handler according to version."""
    request = context.get("request")
    if f"{api_path_prefix()}v2/" in request.path:
        return custom_exception_handler_v2(exc, context)
    else:
        return custom_exception_handler(exc, context)


def custom_exception_handler(exc, context):
    """Create custom response for exceptions."""
    response = exception_handler(exc, context)

    # Now add the HTTP status code to the response.
    if response is not None:
        errors = []
        data = copy.deepcopy(response.data)
        if isinstance(data, dict):
            errors += _generate_errors_from_dict(data, **{"status_code": str(response.status_code)})
        elif isinstance(data, list):
            errors += _generate_errors_from_list(data, **{"status_code": str(response.status_code)})
        error_response = {"errors": errors}
        response.data = error_response
    elif isinstance(exc, IntegrityError):
        source_view = context.get("view")
        response = Response(
            {
                "errors": [
                    {"detail": str(exc), "source": f"{source_view.basename}", "status": "400"},
                ],
            },  # noqa: E231
            status=status.HTTP_400_BAD_REQUEST,
        )
    elif isinstance(exc, InvalidTokenError):
        response = Response(
            data=_generate_error_data_payload_response(
                detail="Invalid token provided.", context=context, http_status_code=status.HTTP_401_UNAUTHORIZED
            ),
            content_type="application/json",
            status=status.HTTP_401_UNAUTHORIZED,
        )
    elif isinstance(exc, MissingAuthorizationError):
        response = Response(
            data=_generate_error_data_payload_response(
                detail="A Bearer token in an authorization header is required when performing service account"
                " operations.",
                context=context,
                http_status_code=status.HTTP_401_UNAUTHORIZED,
            ),
            content_type="application/json",
            status=status.HTTP_401_UNAUTHORIZED,
        )
    elif isinstance(exc, UnableMeetPrerequisitesError):
        response = Response(
            data=_generate_error_data_payload_response(
                detail="Unable to validate the provided token.",
                context=context,
                http_status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            ),
            content_type="application/json",
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return response
