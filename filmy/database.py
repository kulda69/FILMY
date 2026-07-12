from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar

import duckdb

from filmy.paths import DB_PATH


DUCKDB_RETRY_ATTEMPTS = 8
DUCKDB_RETRY_BASE_DELAY_SECONDS = 0.25

ResultT = TypeVar("ResultT")


def is_duckdb_lock_error(exc: duckdb.Error) -> bool:
    """Return whether a DuckDB error is a transient file-lock collision."""
    message = str(exc).lower()
    return "could not set lock on file" in message or "can't open a connection to same database file" in message


@contextmanager
def open_duckdb_connection(*, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open one DuckDB connection through the shared backend boundary."""
    with duckdb.connect(DB_PATH.as_posix(), read_only=read_only) as conn:
        yield conn


def run_duckdb_write(action: Callable[[duckdb.DuckDBPyConnection], ResultT]) -> ResultT:
    """Run one atomic write, retrying locks only before commit starts.

    A lock raised while opening, beginning, or executing the callback is safe
    to retry after rollback.  Any error raised by ``COMMIT`` is propagated
    immediately because the transaction outcome may already be durable; rerunning
    the callback could therefore duplicate a successful write.
    """
    last_error: duckdb.Error | None = None
    for attempt in range(DUCKDB_RETRY_ATTEMPTS):
        commit_started = False
        try:
            with open_duckdb_connection(read_only=False) as conn:
                conn.execute("BEGIN TRANSACTION")
                try:
                    result = action(conn)
                except BaseException:
                    _rollback_preserving_error(conn)
                    raise

                commit_started = True
                conn.execute("COMMIT")
                return result
        except duckdb.Error as exc:
            if commit_started or not is_duckdb_lock_error(exc):
                raise
            last_error = exc
            if attempt == DUCKDB_RETRY_ATTEMPTS - 1:
                break
            time.sleep(DUCKDB_RETRY_BASE_DELAY_SECONDS * (attempt + 1))
    assert last_error is not None
    raise last_error


def run_duckdb_read(action: Callable[[duckdb.DuckDBPyConnection], ResultT]) -> ResultT:
    """Run one read action with the existing bounded lock retry policy."""
    return _run_with_lock_retry(action, read_only=True)


def _rollback_preserving_error(conn: duckdb.DuckDBPyConnection) -> None:
    """Roll back after a callback failure without hiding that original error."""
    try:
        conn.execute("ROLLBACK")
    except duckdb.Error:
        pass


def _run_with_lock_retry(
    action: Callable[[duckdb.DuckDBPyConnection], ResultT],
    *,
    read_only: bool,
) -> ResultT:
    last_error: duckdb.Error | None = None
    for attempt in range(DUCKDB_RETRY_ATTEMPTS):
        try:
            with open_duckdb_connection(read_only=read_only) as conn:
                return action(conn)
        except duckdb.Error as exc:
            if not is_duckdb_lock_error(exc):
                raise
            last_error = exc
            if attempt == DUCKDB_RETRY_ATTEMPTS - 1:
                break
            time.sleep(DUCKDB_RETRY_BASE_DELAY_SECONDS * (attempt + 1))
    assert last_error is not None
    raise last_error
