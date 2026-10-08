#
# Copyright 2026 Red Hat, Inc.
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
"""Helper utilities for ProblemDetails response."""

import enum
from http import HTTPStatus
from typing import Optional

from rest_framework.response import Response


class ProblemType(enum.StrEnum):
    """Enum of known Kessel problem types."""

    INVALID_REQUEST = "http://project-kessel.org/problems/invalid-request"
    UNAUTHENTICATED = "http://project-kessel.org/problems/unauthenticated"
    INSUFFICIENT_PERMISSION = "http://project-kessel.org/problems/insufficient-permission"
    NOT_FOUND = "http://project-kessel.org/problems/not-found"
    CONFLICT = "http://project-kessel.org/problems/conflict"
    INTERNAL_ERROR = "http://project-kessel.org/problems/internal-error"
    ALREADY_EXISTS = "http://project-kessel.org/problems/already-exists"


PROBLEM_TYPE_TITLES = {
    ProblemType.INVALID_REQUEST: "The request payload contains invalid syntax.",
    ProblemType.UNAUTHENTICATED: "Authentication credentials were not provided or are invalid.",
    ProblemType.INSUFFICIENT_PERMISSION: "You do not have permission to perform this action.",
    ProblemType.NOT_FOUND: "Not found.",
    ProblemType.CONFLICT: "Conflict.",
    ProblemType.INTERNAL_ERROR: "Unexpected error occurred.",
    ProblemType.ALREADY_EXISTS: "The resource already exists.",
}

# RFC 9457 problem type URIs matching the TypeSpec ProblemType enum.
# Each URI identifies a specific problem category for machine-readable error handling.
STATUS_PROBLEM_TYPES = {
    400: ProblemType.INVALID_REQUEST,
    401: ProblemType.UNAUTHENTICATED,
    403: ProblemType.INSUFFICIENT_PERMISSION,
    404: ProblemType.NOT_FOUND,
    409: ProblemType.CONFLICT,
    500: ProblemType.INTERNAL_ERROR,
}


def status_default_problem_title(status_code: int) -> str:
    """Get the title for the default problem type for the provided status code."""
    return PROBLEM_TYPE_TITLES[STATUS_PROBLEM_TYPES[status_code]]


def problem_response_body(
    status_code: int,
    *,
    problem_type: Optional[str] = None,
    detail: str,
    instance: Optional[str] = None,
    extra_data: Optional[dict] = None,
):
    """Create an RFC 9457-compliant response body."""
    if not isinstance(status_code, int):
        raise TypeError(f"Expected status_code to be an int, but got: {status_code!r}")

    if not (400 <= status_code < 600):
        raise ValueError(f"Invalid status code: {status_code}")

    if problem_type is None:
        # If we do not have a specific problem type for the status, we will leave problem_type as None and eventually
        # omit it, which, per RFC 9457, means clients should just look at the status code.
        problem_type = STATUS_PROBLEM_TYPES.get(status_code)

    title = PROBLEM_TYPE_TITLES[problem_type] if problem_type is not None else HTTPStatus(status_code).phrase

    result = dict(extra_data) if extra_data is not None else {}

    for key in ["status", "type", "title", "detail", "instance"]:
        if key in result:
            raise ValueError(f"Reserved key {key} in extra_data: {extra_data}")

    result["status"] = status_code
    result["title"] = title
    result["detail"] = detail

    if problem_type is not None:
        result["type"] = problem_type

    if instance is not None:
        result["instance"] = instance

    return result


def problem_response(status_code: int, **kwargs):
    """
    Return a full Response object with an RFC 9457-compliant body.

    See problem_response_body for full arguments.
    """
    return Response(
        status=status_code,
        content_type="application/problem+json",
        data=problem_response_body(status_code=status_code, **kwargs),
    )


def v2response_error_from_errors(errors, exc=None, context=None, problem_type=None):
    """Build a ProblemDetails-formatted error response from errors.

    Args:
        errors: List of error dicts with "detail", "status", and optional "source" keys.
        exc: The original exception (optional).
        context: DRF context dict with "request" (optional).
        problem_type: Explicit RFC 9457 problem type URI override. When set, this
            takes precedence over the default status-code-based lookup in PROBLEM_TYPES.
            Use for specialized problem types like ALREADY_EXISTS.
    """
    detail = ""
    status_code = 0
    field_errors = []

    if errors and any(isinstance(error, dict) and "detail" in error for error in errors):
        detail = str(errors[0]["detail"])
        status_code = int(errors[0]["status"])

        for error in errors:
            if isinstance(error, dict) and "detail" in error:
                field_error = {"message": str(error["detail"])}
                if error.get("source"):
                    field_error["field"] = error["source"]
                field_errors.append(field_error)

    instance = (
        context.get("request").path
        if (context and context.get("request") and context.get("request").method in ["PUT", "PATCH", "DELETE"])
        else None
    )

    extra_data = {"errors": field_errors} if field_errors else None

    return problem_response_body(
        status_code=status_code, problem_type=problem_type, detail=detail, instance=instance, extra_data=extra_data
    )
