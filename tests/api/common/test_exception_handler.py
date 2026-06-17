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
"""Test the API exception handler module."""

import uuid

from django.db import IntegrityError
from django.test import TestCase
from management.authorization.invalid_token import InvalidTokenError
from management.authorization.missing_authorization import MissingAuthorizationError
from management.authorization.unable_meet_prerequisites import UnableMeetPrerequisitesError
from management.exceptions import InvalidFieldError, NotFoundError, RequiredFieldError
from management.role.v2_exceptions import RolesNotFoundError
from rest_framework import status
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.views import Response
from unittest.mock import Mock

from api.common.exception_handler import custom_exception_handler, custom_exception_handler_v2
from api.common.exception_handler import _generate_errors_from_dict, _generate_error_data_payload_response
from management.utils import v2response_error_from_errors, PROBLEM_TITLES


class ExceptionHandlerTest(TestCase):
    """Tests against the exception handler functions."""

    def test_generate_errors_from_dict(self):
        """Test generating errors from dictionary errors."""
        kwargs = {"status_code": 400}
        response = {"non_field_errors": ["Cannot access AWS bucket with ARN", "ARN format is incorrect"]}
        formatted_errors = _generate_errors_from_dict(response, **kwargs)
        expected = [
            {"detail": "Cannot access AWS bucket with ARN", "source": "non_field_errors", "status": 400},
            {"detail": "ARN format is incorrect", "source": "non_field_errors", "status": 400},
        ]
        self.assertEqual(formatted_errors, expected)

        kwargs = {"status_code": 400}
        response = {"provider_type": "Must be either OCP or AWS"}
        formatted_errors = _generate_errors_from_dict(response, **kwargs)
        expected = [{"detail": "Must be either OCP or AWS", "source": "provider_type", "status": 400}]
        self.assertEqual(formatted_errors, expected)

        kwargs = {"status_code": 400}
        response = {"tiered_rate": {"unit": ['"UD" is not a valid choice.']}}
        formatted_errors = _generate_errors_from_dict(response, **kwargs)
        expected = [{"detail": '"UD" is not a valid choice.', "source": "tiered_rate.unit", "status": 400}]
        self.assertEqual(formatted_errors, expected)

        kwargs = {"status_code": 400}
        response = {"tiered_rate": {"value": ["Ensure that there are no more than 10 decimal places."]}}
        formatted_errors = _generate_errors_from_dict(response, **kwargs)
        expected = [
            {
                "detail": "Ensure that there are no more than 10 decimal places.",
                "source": "tiered_rate.value",
                "status": 400,
            }
        ]
        self.assertEqual(formatted_errors, expected)

        kwargs = {"status_code": 400}
        response = {"tiered_rate": {"value": {"key": "Ensure that there are no more than 10 decimal places."}}}
        formatted_errors = _generate_errors_from_dict(response, **kwargs)
        expected = [
            {
                "detail": "Ensure that there are no more than 10 decimal places.",
                "source": "tiered_rate.value.key",
                "status": 400,
            }
        ]
        self.assertEqual(formatted_errors, expected)

        kwargs = {"status_code": 400}
        response = {"tiered_rate": {"value": [["key"], ["Ensure that there are no more than 10 decimal places."]]}}
        formatted_errors = _generate_errors_from_dict(response, **kwargs)
        expected = [
            {"detail": "key", "source": "tiered_rate.value", "status": 400},
            {
                "detail": "Ensure that there are no more than 10 decimal places.",
                "source": "tiered_rate.value",
                "status": 400,
            },
        ]
        self.assertEqual(formatted_errors, expected)

    def test_invalid_token_exception_handled(self):
        """Test that an "invalid token" exception gets properly handled."""
        # Mock the view and the context.
        mocked_view = Mock()
        mocked_view.basename = "some-view-handler"

        context = {"view": mocked_view}

        # Call the function under test.
        response: Response = custom_exception_handler(exc=InvalidTokenError(), context=context)

        # Assert that the correct response was returned for the exception.
        self.assertEqual(
            status.HTTP_401_UNAUTHORIZED,
            response.status_code,
            "unexpected status code in the response for the 'InvalidTokenError' exception handling",
        )

        self.assertEqual(
            "Invalid token provided.",
            str(response.data.get("errors")[0].get("detail")),
            "unexpected error message in the response for the 'InvalidTokenError' exception handling",
        )

        self.assertEqual(
            mocked_view.basename,
            str(response.data.get("errors")[0].get("source")),
            "unexpected source view in the response for the 'MissingAuthorizationError' exception handling",
        )

    def test_missing_authorization_exception_handled(self):
        """Test that a "missing authorization" exception gets properly handled."""
        # Mock the view and the context.
        mocked_view = Mock()
        mocked_view.basename = "some-view-handler"

        context = {"view": mocked_view}

        # Call the function under test.
        response: Response = custom_exception_handler(exc=MissingAuthorizationError(), context=context)

        # Assert that the correct response was returned for the exception.
        self.assertEqual(
            status.HTTP_401_UNAUTHORIZED,
            response.status_code,
            "unexpected status code in the response for the 'MissingAuthorizationError' exception handling",
        )

        self.assertEqual(
            "A Bearer token in an authorization header is required when performing service account operations.",
            str(response.data.get("errors")[0].get("detail")),
            "unexpected error message in the response for the 'MissingAuthorizationError' exception handling",
        )

        self.assertEqual(
            mocked_view.basename,
            str(response.data.get("errors")[0].get("source")),
            "unexpected source view in the response for the 'MissingAuthorizationError' exception handling",
        )

    def test_unable_meet_prerequisites_exception_handled(self):
        """Test that an "unable to meet prerequisites" exception gets properly handled."""
        # Mock the view and the context.
        mocked_view = Mock()
        mocked_view.basename = "some-view-handler"

        context = {"view": mocked_view}

        # Call the function under test.
        response: Response = custom_exception_handler(exc=UnableMeetPrerequisitesError(), context=context)

        # Assert that the correct response was returned for the exception.
        self.assertEqual(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            response.status_code,
            "unexpected status code in the response for the 'UnableMeetPrerequisitesError' exception handling",
        )

        self.assertEqual(
            "Unable to validate the provided token.",
            str(response.data.get("errors")[0].get("detail")),
            "unexpected error message in the response for the 'UnableMeetPrerequisitesError' exception handling",
        )

        self.assertEqual(
            mocked_view.basename,
            str(response.data.get("errors")[0].get("source")),
            "unexpected source view in the response for the 'UnableMeetPrerequisitesError' exception handling",
        )

    def test_generate_error_data_payload_with_view_response(self):
        """Tests that the function under test generates the data payload correctly when a view is passed in the context."""
        # Prepare a payload with a view in the context.
        detail = "some error message"
        mocked_view = Mock()
        mocked_view.basename = "some-view-handler"
        context = {"view": mocked_view}
        http_status_code = status.HTTP_200_OK

        # Call the function under test.
        result = _generate_error_data_payload_response(
            detail=detail, context=context, http_status_code=http_status_code
        )

        # Assert that the correct structure is returned.
        errors = result.get("errors")
        if not errors:
            self.fail(f"the errors array was not present in the payload: {result}")

        if len(errors) != 1:
            self.fail(f"only one error was expected in the payload: {result}")

        only_error = errors[0]

        self.assertEqual(detail, only_error.get("detail"), f"unexpected detail message in the payload: {result}")

        self.assertEqual(mocked_view.basename, only_error.get("source"), f"unexpected source in the payload: {result}")

        self.assertEqual(
            str(http_status_code), only_error.get("status"), f"unexpected status code in the payload: {result}"
        )

    def test_generate_error_data_payload_without_view_basename(self):
        """
        Tests that the function under test generates the data payload correctly
        when a view is passed in the context without basename attribute."""
        # Prepare a payload with a view in the context without "basename" attribute.
        detail = "some error message"
        context = {"view": []}
        http_status_code = status.HTTP_200_OK

        # Call the function under test.
        result = _generate_error_data_payload_response(
            detail=detail, context=context, http_status_code=http_status_code
        )

        # Assert that the correct structure is returned.
        errors = result.get("errors")
        if not errors:
            self.fail(f"the errors array was not present in the payload: {result}")

        if len(errors) != 1:
            self.fail(f"only one error was expected in the payload: {result}")

        only_error = errors[0]

        self.assertEqual(detail, only_error.get("detail"), f"unexpected detail message in the payload: {result}")
        self.assertIsNone(only_error.get("source"), f"unexpected 'source' in the payload: {result}")
        self.assertEqual(
            str(http_status_code), only_error.get("status"), f"unexpected status code in the payload: {result}"
        )

    def test_generate_error_data_payload_without_view_response(self):
        """Tests that the function under test generates the data payload correctly when no view is passed in the context."""
        # Prepare a payload without a view in the context.
        detail = "some error message"
        context = {}
        http_status_code = status.HTTP_200_OK

        # Call the function under test.
        result = _generate_error_data_payload_response(
            detail=detail, context=context, http_status_code=http_status_code
        )

        # Assert that the correct structure is returned.
        errors = result.get("errors")
        if not errors:
            self.fail(f"the errors array was not present in the payload: {result}")

        if len(errors) != 1:
            self.fail(f"only one error was expected in the payload: {result}")

        only_error = errors[0]

        self.assertEqual(detail, only_error.get("detail"), f"unexpected detail message in the payload: {result}")

        self.assertEqual(
            str(http_status_code), only_error.get("status"), f"unexpected status code in the payload: {result}"
        )


class V2ProblemDetailsTest(TestCase):
    """Tests for v2 ProblemDetails format conformance."""

    def _mock_context(self, method="POST"):
        """Create a mock context with request."""
        mock_request = Mock()
        mock_request.method = method
        mock_request.path = "/api/v2/roles/"
        return {"request": mock_request}

    def test_v2response_includes_status_title_detail(self):
        """Test that v2 response includes all ProblemDetails fields."""
        errors = [{"detail": "Test error message", "status": "400"}]
        context = self._mock_context()

        result = v2response_error_from_errors(errors, context=context)

        self.assertIn("status", result)
        self.assertIn("title", result)
        self.assertIn("detail", result)
        self.assertEqual(result["status"], 400)
        self.assertEqual(result["detail"], "Test error message")

    def test_v2response_400_has_correct_title(self):
        """Test that 400 errors have the correct title."""
        errors = [{"detail": "Invalid input", "status": "400"}]
        context = self._mock_context()

        result = v2response_error_from_errors(errors, context=context)

        self.assertEqual(result["title"], PROBLEM_TITLES[400])

    def test_v2response_401_has_correct_title(self):
        """Test that 401 errors have the correct title."""
        errors = [{"detail": "Not authenticated", "status": "401"}]
        context = self._mock_context()

        result = v2response_error_from_errors(errors, context=context)

        self.assertEqual(result["title"], PROBLEM_TITLES[401])

    def test_v2response_403_has_correct_title(self):
        """Test that 403 errors have the correct title."""
        errors = [{"detail": "Permission denied", "status": "403"}]
        context = self._mock_context()

        result = v2response_error_from_errors(errors, context=context)

        self.assertEqual(result["title"], PROBLEM_TITLES[403])

    def test_v2response_404_has_correct_title(self):
        """Test that 404 errors have the correct title."""
        errors = [{"detail": "Resource not found", "status": "404"}]
        context = self._mock_context()

        result = v2response_error_from_errors(errors, context=context)

        self.assertEqual(result["title"], PROBLEM_TITLES[404])

    def test_v2response_409_has_correct_title(self):
        """Test that 409 errors have the correct title."""
        errors = [{"detail": "Concurrent update conflict", "status": "409"}]
        context = self._mock_context()

        result = v2response_error_from_errors(errors, context=context)

        self.assertEqual(result["title"], PROBLEM_TITLES[409])

    def test_v2response_500_has_correct_title(self):
        """Test that 500 errors have the correct title."""
        errors = [{"detail": "Internal error", "status": "500"}]
        context = self._mock_context()

        result = v2response_error_from_errors(errors, context=context)

        self.assertEqual(result["title"], PROBLEM_TITLES[500])

    def test_v2response_unknown_status_has_fallback_title(self):
        """Test that unknown status codes get a fallback title."""
        errors = [{"detail": "Some error", "status": "418"}]
        context = self._mock_context()

        result = v2response_error_from_errors(errors, context=context)

        self.assertEqual(result["title"], "An error occurred.")

    def test_v2response_includes_instance_for_put(self):
        """Test that PUT requests include instance field."""
        errors = [{"detail": "Update failed", "status": "400"}]
        context = self._mock_context(method="PUT")

        result = v2response_error_from_errors(errors, context=context)

        self.assertIn("instance", result)
        self.assertEqual(result["instance"], "/api/v2/roles/")

    def test_v2response_includes_instance_for_patch(self):
        """Test that PATCH requests include instance field."""
        errors = [{"detail": "Patch failed", "status": "400"}]
        context = self._mock_context(method="PATCH")

        result = v2response_error_from_errors(errors, context=context)

        self.assertIn("instance", result)

    def test_v2response_includes_instance_for_delete(self):
        """Test that DELETE requests include instance field."""
        errors = [{"detail": "Delete failed", "status": "400"}]
        context = self._mock_context(method="DELETE")

        result = v2response_error_from_errors(errors, context=context)

        self.assertIn("instance", result)

    def test_v2response_excludes_instance_for_post(self):
        """Test that POST requests do not include instance field."""
        errors = [{"detail": "Create failed", "status": "400"}]
        context = self._mock_context(method="POST")

        result = v2response_error_from_errors(errors, context=context)

        self.assertNotIn("instance", result)

    def test_v2response_excludes_instance_for_get(self):
        """Test that GET requests do not include instance field."""
        errors = [{"detail": "Get failed", "status": "400"}]
        context = self._mock_context(method="GET")

        result = v2response_error_from_errors(errors, context=context)

        self.assertNotIn("instance", result)


class V2ExceptionHandlerTests(TestCase):
    """Tests for every branch in custom_exception_handler_v2.

    Each test asserts the full response shape (status_code, content_type, data)
    rather than individual attributes.
    """

    PATH = "/api/v2/role-bindings/"

    def _mock_v2_context(self, method="PUT"):
        """Create a mock context with a v2 request."""
        mock_request = Mock()
        mock_request.method = method
        mock_request.path = self.PATH
        return {"request": mock_request}

    # ── Branch: DRF exception (dict data, response is not None) ──────

    def test_drf_validation_error_dict_returns_problem_details(self):
        """Test that a DRF ValidationError with dict payload is wrapped in Problem Details."""
        exc = DRFValidationError({"name": ["This field is required."]})
        context = self._mock_v2_context()

        response = custom_exception_handler_v2(exc, context)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content_type, "application/problem+json")
        self.assertEqual(
            response.data,
            {
                "status": 400,
                "title": PROBLEM_TITLES[400],
                "detail": "This field is required.",
                "errors": [{"message": "This field is required.", "field": "name"}],
                "instance": self.PATH,
            },
        )

    # ── Branch: DRF exception (list data, response is not None) ──────

    def test_drf_validation_error_list_returns_problem_details(self):
        """Test that a DRF ValidationError with list payload is wrapped in Problem Details."""
        exc = DRFValidationError(["Error one.", "Error two."])
        context = self._mock_v2_context()

        response = custom_exception_handler_v2(exc, context)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content_type, "application/problem+json")
        self.assertEqual(
            response.data,
            {
                "status": 400,
                "title": PROBLEM_TITLES[400],
                "detail": "Error one.",
                "errors": [{"message": "Error one."}, {"message": "Error two."}],
                "instance": self.PATH,
            },
        )

    # ── Branch: IntegrityError ────────────────────────────────────────

    def test_integrity_error_returns_400(self):
        """Test that an IntegrityError produces a 400 Problem Details response."""
        exc = IntegrityError("duplicate key value violates unique constraint")
        mock_view = Mock()
        mock_view.basename = "role-bindings"
        context = self._mock_v2_context()
        context["view"] = mock_view

        response = custom_exception_handler_v2(exc, context)

        detail = "duplicate key value violates unique constraint"
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content_type, "application/problem+json")
        self.assertEqual(
            response.data,
            {
                "status": 400,
                "title": PROBLEM_TITLES[400],
                "detail": detail,
                "errors": [{"message": detail, "field": "role-bindings"}],
                "instance": self.PATH,
            },
        )

    # ── Branch: InvalidTokenError ─────────────────────────────────────

    def test_invalid_token_error_returns_401(self):
        """Test that InvalidTokenError produces a 401 Problem Details response."""
        exc = InvalidTokenError()
        context = self._mock_v2_context()

        response = custom_exception_handler_v2(exc, context)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.data,
            {
                "status": 401,
                "title": PROBLEM_TITLES[401],
                "detail": "Invalid token provided.",
                "errors": [{"message": "Invalid token provided."}],
            },
        )

    # ── Branch: MissingAuthorizationError ─────────────────────────────

    def test_missing_authorization_error_returns_401(self):
        """Test that MissingAuthorizationError produces a 401 Problem Details response."""
        exc = MissingAuthorizationError()
        context = self._mock_v2_context()

        response = custom_exception_handler_v2(exc, context)

        detail = "A Bearer token in an authorization header is required" " when performing service account operations."
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.data,
            {
                "status": 401,
                "title": PROBLEM_TITLES[401],
                "detail": detail,
                "errors": [{"message": detail}],
            },
        )

    # ── Branch: UnableMeetPrerequisitesError ──────────────────────────

    def test_unable_meet_prerequisites_error_returns_500(self):
        """Test that UnableMeetPrerequisitesError produces a 500 Problem Details response."""
        exc = UnableMeetPrerequisitesError()
        context = self._mock_v2_context()

        response = custom_exception_handler_v2(exc, context)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.data,
            {
                "status": 500,
                "title": PROBLEM_TITLES[500],
                "detail": "Unable to validate the provided token.",
                "errors": [{"message": "Unable to validate the provided token."}],
            },
        )

    # ── Branch: RolesNotFoundError ─────────────────────────────────────

    def test_role_not_found_error_returns_404_problem_details(self):
        """Test that RolesNotFoundError produces a 404 Problem Details response."""
        role_uuid = str(uuid.uuid4())
        exc = RolesNotFoundError([role_uuid])
        context = self._mock_v2_context()

        response = custom_exception_handler_v2(exc, context)

        detail = f"Role with UUID '{role_uuid}' not found."
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.content_type, "application/problem+json")
        self.assertEqual(
            response.data,
            {
                "status": 404,
                "title": PROBLEM_TITLES[404],
                "detail": detail,
                "errors": [{"message": detail, "field": "detail"}],
                "instance": self.PATH,
            },
        )

    # ── Branch: NotFoundError ─────────────────────────────────────────

    def test_not_found_error_returns_404_problem_details(self):
        """Test that NotFoundError produces a 404 Problem Details response."""
        exc = NotFoundError(resource_type="workspace", resource_id="abc-123")
        context = self._mock_v2_context()

        response = custom_exception_handler_v2(exc, context)

        detail = "workspace with id 'abc-123' not found"
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.content_type, "application/problem+json")
        self.assertEqual(
            response.data,
            {
                "status": 404,
                "title": PROBLEM_TITLES[404],
                "detail": detail,
                "errors": [{"message": detail, "field": "detail"}],
                "instance": self.PATH,
            },
        )

    # ── Branch: InvalidFieldError ─────────────────────────────────────

    def test_invalid_field_error_returns_400_problem_details(self):
        """Test that InvalidFieldError produces a 400 Problem Details response with field source."""
        exc = InvalidFieldError(field="roles", message="roles do not exist: role-1")
        context = self._mock_v2_context()

        response = custom_exception_handler_v2(exc, context)

        detail = "Invalid field 'roles': roles do not exist: role-1"
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content_type, "application/problem+json")
        self.assertEqual(
            response.data,
            {
                "status": 400,
                "title": PROBLEM_TITLES[400],
                "detail": detail,
                "errors": [{"message": detail, "field": "roles"}],
                "instance": self.PATH,
            },
        )

    # ── Branch: RequiredFieldError ────────────────────────────────────

    def test_required_field_error_returns_400_problem_details(self):
        """Test that RequiredFieldError produces a 400 Problem Details response with field source."""
        exc = RequiredFieldError("resource_type")
        context = self._mock_v2_context()

        response = custom_exception_handler_v2(exc, context)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content_type, "application/problem+json")
        self.assertEqual(
            response.data,
            {
                "status": 400,
                "title": PROBLEM_TITLES[400],
                "detail": "resource_type is required",
                "errors": [{"message": "resource_type is required", "field": "resource_type"}],
                "instance": self.PATH,
            },
        )

    # ── Branch: unhandled exception ───────────────────────────────────

    def test_unhandled_exception_returns_none(self):
        """Test that an unrecognized exception returns None (Django handles it)."""
        exc = RuntimeError("something unexpected")
        context = self._mock_v2_context()

        response = custom_exception_handler_v2(exc, context)

        self.assertIsNone(response)
