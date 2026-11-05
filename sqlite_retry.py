"""Small, central retry layer for transient SQLite access failures.

The application keeps its SQLite database on a Docker Desktop bind mount.
Short lived I/O/lock errors can therefore occur while a tracker thread writes
and a web request reads at the same time.  This module retries only those
transient access errors; SQL syntax, constraint and data errors are still
raised immediately.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any, Callable, TypeVar


_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")

SQLITE_RETRY_ATTEMPTS = max(1, int(os.getenv("SQLITE_RETRY_ATTEMPTS", "5")))
SQLITE_RETRY_DELAY_SECONDS = max(
    0.02, float(os.getenv("SQLITE_RETRY_DELAY_SECONDS", "0.15"))
)
SQLITE_RETRY_MAX_DELAY_SECONDS = max(
    SQLITE_RETRY_DELAY_SECONDS,
    float(os.getenv("SQLITE_RETRY_MAX_DELAY_SECONDS", "1.0")),
)

_TRANSIENT_MESSAGES = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "disk i/o error",
    "unable to open database file",
    "resource temporarily unavailable",
)


def is_transient_sqlite_error(error: BaseException) -> bool:
    """Return whether *error* is a short-lived SQLite access failure."""

    if not isinstance(error, sqlite3.OperationalError):
        return False
    message = str(error).casefold()
    return any(fragment in message for fragment in _TRANSIENT_MESSAGES)


def retry_sqlite_operation(
    operation: Callable[[], _T],
    *,
    operation_name: str = "sqlite operation",
) -> _T:
    """Run a SQLite operation with a short bounded retry window.

    Only transient access errors are retried.  The final exception is raised
    unchanged so callers still get the real failure if the database or volume
    is genuinely unavailable.
    """

    delay = SQLITE_RETRY_DELAY_SECONDS
    for attempt in range(1, SQLITE_RETRY_ATTEMPTS + 1):
        try:
            return operation()
        except sqlite3.OperationalError as error:
            if not is_transient_sqlite_error(error) or attempt >= SQLITE_RETRY_ATTEMPTS:
                raise
            _LOGGER.debug(
                "[SQLite] transient access failure in %s; retry %s/%s in %.2fs: %s",
                operation_name,
                attempt,
                SQLITE_RETRY_ATTEMPTS - 1,
                delay,
                error,
            )
            time.sleep(delay)
            delay = min(delay * 2, SQLITE_RETRY_MAX_DELAY_SECONDS)
    raise RuntimeError("unreachable")


class RetryingCursorMixin:
    """Retry execute methods on cursors created from a retrying connection."""

    def execute(self, sql: str, parameters: Any = ()):  # type: ignore[override]
        base_execute = super().execute
        return retry_sqlite_operation(
            lambda: base_execute(sql, parameters),
            operation_name="cursor.execute",
        )

    def executemany(self, sql: str, parameters: Any):  # type: ignore[override]
        base_executemany = super().executemany
        return retry_sqlite_operation(
            lambda: base_executemany(sql, parameters),
            operation_name="cursor.executemany",
        )

    def executescript(self, sql_script: str):  # type: ignore[override]
        base_executescript = super().executescript
        return retry_sqlite_operation(
            lambda: base_executescript(sql_script),
            operation_name="cursor.executescript",
        )


class RetryingConnectionMixin:
    """Connection methods shared by the history and inventory repositories."""

    def execute(self, sql: str, parameters: Any = (), /):  # type: ignore[override]
        base_execute = super().execute
        return retry_sqlite_operation(
            lambda: base_execute(sql, parameters),
            operation_name="connection.execute",
        )

    def executemany(self, sql: str, parameters: Any, /):  # type: ignore[override]
        base_executemany = super().executemany
        return retry_sqlite_operation(
            lambda: base_executemany(sql, parameters),
            operation_name="connection.executemany",
        )

    def executescript(self, sql_script: str, /):  # type: ignore[override]
        base_executescript = super().executescript
        return retry_sqlite_operation(
            lambda: base_executescript(sql_script),
            operation_name="connection.executescript",
        )

    def commit(self):  # type: ignore[override]
        base_commit = super().commit
        return retry_sqlite_operation(
            base_commit,
            operation_name="connection.commit",
        )

    def cursor(self, factory=None):  # type: ignore[override]
        return super().cursor(factory or _RetryingCursor)


class _RetryingCursor(RetryingCursorMixin, sqlite3.Cursor):
    """Concrete cursor class used by :class:`RetryingConnectionMixin`."""

