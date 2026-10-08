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
from typing import Any, Iterable, Optional

from django.db import IntegrityError
from django.http import Http404
from management.authorization.invalid_token import InvalidTokenError
from management.authorization.missing_authorization import MissingAuthorizationError
from management.authorization.unable_meet_prerequisites import UnableMeetPrerequisitesError
from management.exceptions import InvalidFieldError, InventoryAuthUnavailableError, NotFoundError, RequiredFieldError
from management.problem_details import single_problem_response_with_errors_for_context
from management.role.v2_exceptions import RolesNotFoundError
from management.utils import api_path_prefix, v2response_error_from_errors
from rest_framework import status
from rest_framework.views import Response, exception_handler


def _items_with_source(data, base_source: Optional[str] = None) -> Iterable[tuple[Optional[str], Any]]:
    if isinstance(data, dict):
        return (((f"{base_source}.{key}" if base_source else key), value) for key, value in data.items())

    if isinstance(data, list):
        return ((base_source, value) for value in data)

    raise TypeError(f"Expected data to be a dict or a list, but got: {data!r}")


def _flatten_v1_errors(data, *, source: Optional[str] = None, status_code: str):
    errors = []

    for nested_source, value in _items_with_source(data, base_source=source):
        if isinstance(value, str):
            new_error = {"detail": value, "source": nested_source, "status": status_code}
            errors.append(new_error)
        elif isinstance(value, list) or isinstance(value, dict):
            errors += _flatten_v1_errors(value, source=nested_source, status_code=status_code)
        else:
            raise TypeError(f"Unexpected error value: {value!r}")

    return errors


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


def _v1_response_to_v2(response: Response, context: dict) -> Response:
    response.content_type = "application/problem+json"
    errors = []
    data = copy.deepcopy(response.data)

    if isinstance(data, dict) or isinstance(data, list):
        errors += _flatten_v1_errors(data, status_code=str(response.status_code))

    response.data = v2response_error_from_errors(errors=errors, context=context)

    return response


def custom_exception_handler_v2(exc, context):
    """Create custom response for v2 exceptions."""
    v1_initial_response = exception_handler(exc, context)

    # Now add the HTTP status code to the response.
    if v1_initial_response is not None:
        return _v1_response_to_v2(v1_initial_response, context)
    elif isinstance(exc, IntegrityError):
        return single_problem_response_with_errors_for_context(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
            context=context,
            with_instance=True,
        )
    elif isinstance(exc, InvalidTokenError):
        return single_problem_response_with_errors_for_context(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token provided.",
            context=context,
        )
    elif isinstance(exc, MissingAuthorizationError):
        return single_problem_response_with_errors_for_context(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A Bearer token in an authorization header is required when performing service account operations.",
            context=context,
        )
    elif isinstance(exc, UnableMeetPrerequisitesError):
        return single_problem_response_with_errors_for_context(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to validate the provided token.",
            context=context,
        )
    elif isinstance(exc, InventoryAuthUnavailableError):
        response = single_problem_response_with_errors_for_context(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inventory authorization is temporarily unavailable. Please try again shortly.",
            context=context,
        )
        response["Retry-After"] = "1"
        return response
    elif isinstance(exc, RolesNotFoundError):
        # Convert RolesNotFoundError to Http404 and let standard handler process it
        v1_response = exception_handler(Http404(str(exc)), context)
        return _v1_response_to_v2(v1_response, context)
    elif isinstance(exc, NotFoundError):
        v1_response = exception_handler(Http404(str(exc)), context)
        return _v1_response_to_v2(v1_response, context)
    elif isinstance(exc, (InvalidFieldError, RequiredFieldError)):
        return single_problem_response_with_errors_for_context(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
            context=context,
            source=getattr(exc, "field", None) or getattr(exc, "field_name", None),
            with_instance=True,
        )

    return None


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
        if isinstance(data, dict) or isinstance(data, list):
            errors += _flatten_v1_errors(data, status_code=str(response.status_code))
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
    elif isinstance(exc, InventoryAuthUnavailableError):
        response = Response(
            data=_generate_error_data_payload_response(
                detail="Inventory authorization is temporarily unavailable. Please try again shortly.",
                context=context,
                http_status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ),
            content_type="application/json",
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        response["Retry-After"] = "1"

    return response
