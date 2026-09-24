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

"""Utilities for handling metrics."""

import contextlib
import typing
from collections.abc import Generator

from prometheus_client import Counter


class ResultMetricContext(typing.Protocol):
    """The context returned by track_result_metric."""

    def __call__(self, /, new_result: str):
        """
        Set the result of block to the provided string.

        A subsequent call overwrites the result from any prior one.
        """
        ...

    def exceptionally(self, /, error_result: str, exc_type: type = Exception) -> typing.ContextManager[None]:
        """
        Get a context manager that set what the result will be for any exception that propagates out of its block.

        The result will only be used if the same exception propagates to the track_result_metric block (e.g. it will not
        be used it the exception is caught or a difference exception is re-raised outside this block).
        """
        ...


@contextlib.contextmanager
def track_result_metric(metric: Counter) -> Generator[ResultMetricContext, None, None]:
    """
    Get a context manager that records the result of the block into the provided metric.

    The context manager evaluates to a ResultMetricContext when used in a with statement. The result (set with that
    context) is used as the result label for the metric.
    """
    result = "success"
    result_by_exception = {}

    class _Impl:
        def __call__(self, /, new_result: str):
            nonlocal result
            result = new_result

        @contextlib.contextmanager
        def exceptionally(self, /, error_result: str, exc_type: type = Exception):
            try:
                yield
            except exc_type as e:
                result_by_exception[e] = error_result
                raise

    try:
        yield _Impl()
    except BaseException as e:
        result = result_by_exception.get(e, "error")
        raise
    finally:
        metric.labels(result=result).inc()
