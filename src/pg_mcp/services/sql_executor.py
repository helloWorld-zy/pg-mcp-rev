"""SQL executor for PostgreSQL queries.

This module provides safe SQL execution with session parameter configuration,
result serialization, row limiting, and retry logic for transient errors.
"""

import asyncio
import datetime
import decimal
import logging
import uuid
from typing import Any

import asyncpg
from asyncpg import Connection, Pool

from pg_mcp.config.settings import DatabaseConfig, ResilienceConfig, SecurityConfig
from pg_mcp.models.errors import DatabaseError, ExecutionTimeoutError

logger = logging.getLogger(__name__)

# Transient PostgreSQL error codes that warrant retry
RETRYABLE_ERROR_CODES = {
    "08000",  # Connection exception
    "08003",  # Connection does not exist
    "08006",  # Connection failure
    "08001",  # SQL client unable to establish connection
    "08004",  # Server rejected connection
    "40001",  # Serialization failure
    "40P01",  # Deadlock detected
    "53300",  # Too many connections
    "57P01",  # Admin shutdown
    "57P02",  # Crash shutdown
    "57P03",  # Cannot connect now
}


class SQLExecutor:
    """SQL executor using asyncpg with security measures.

    This executor ensures safe query execution by:
    1. Setting session parameters (timeout, search_path, role)
    2. Running queries in read-only transactions
    3. Limiting the number of returned rows
    4. Serializing PostgreSQL-specific data types
    5. Retrying on transient errors with exponential backoff

    Example:
        >>> executor = SQLExecutor(pool, security_config, db_config)
        >>> results, count = await executor.execute("SELECT * FROM users")
        >>> print(f"Retrieved {count} rows")
    """

    def __init__(
        self,
        pool: Pool,
        security_config: SecurityConfig,
        db_config: DatabaseConfig,
        resilience_config: ResilienceConfig | None = None,
    ) -> None:
        """Initialize SQL executor.

        Args:
            pool: asyncpg connection pool for database connections.
            security_config: Security configuration including timeouts and limits.
            db_config: Database configuration including connection parameters.
            resilience_config: Optional resilience config for retry behavior.
        """
        self.pool = pool
        self.security_config = security_config
        self.db_config = db_config
        self.resilience_config = resilience_config

        # Retry configuration with defaults
        if resilience_config:
            self.max_retries = resilience_config.max_retries
            self.retry_delay = resilience_config.retry_delay
            self.backoff_factor = resilience_config.backoff_factor
        else:
            self.max_retries = 3
            self.retry_delay = 1.0
            self.backoff_factor = 2.0

    async def execute(
        self,
        sql: str,
        timeout: float | None = None,  # noqa: ASYNC109
        max_rows: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Execute SQL query with security measures and retry logic.

        This method:
        1. Acquires a connection from the pool
        2. Starts a read-only transaction
        3. Sets session parameters (timeout, search_path, role)
        4. Executes the query with timeout
        5. Retries on transient errors with exponential backoff
        6. Limits the number of returned rows
        7. Serializes special PostgreSQL types

        Args:
            sql: SQL query to execute (should already be validated).
            timeout: Query timeout in seconds (uses config default if None).
            max_rows: Maximum rows to return (uses config default if None).

        Returns:
            tuple: (results, total_row_count) where:
                - results: List of row dictionaries with serialized values
                - total_row_count: Total number of rows (before limiting)

        Raises:
            ExecutionTimeoutError: If query execution exceeds timeout.
            DatabaseError: If database operation fails after all retries.

        Example:
            >>> results, count = await executor.execute(
            ...     "SELECT id, name FROM users WHERE active = true",
            ...     timeout=10.0,
            ...     max_rows=1000
            ... )
            >>> print(f"Retrieved {len(results)} of {count} total rows")
        """
        # Use configured defaults if not specified
        timeout = timeout or self.security_config.max_execution_time
        max_rows = max_rows or self.security_config.max_rows

        last_error: Exception | None = None
        delay = self.retry_delay

        for attempt in range(self.max_retries + 1):
            try:
                return await self._execute_once(sql, timeout, max_rows)
            except ExecutionTimeoutError:
                # Don't retry timeouts
                raise
            except asyncpg.PostgresError as e:
                # Check if this is a retryable error
                error_code = getattr(e, "sqlstate", None)
                if error_code in RETRYABLE_ERROR_CODES and attempt < self.max_retries:
                    logger.warning(
                        f"Retryable database error (attempt {attempt + 1}/{self.max_retries + 1}): {e}",
                        extra={"error_code": error_code, "retry_delay": delay},
                    )
                    last_error = e
                    await asyncio.sleep(delay)
                    delay *= self.backoff_factor
                    continue
                # Not retryable or out of retries
                raise DatabaseError(
                    message=f"Database query failed: {e!s}",
                    details={
                        "error_code": error_code,
                        "error_message": str(e),
                        "sql": sql[:200],
                        "attempts": attempt + 1,
                    },
                ) from e
            except Exception as e:
                # Don't retry unexpected errors
                raise DatabaseError(
                    message=f"Unexpected error during query execution: {e!s}",
                    details={
                        "error_type": type(e).__name__,
                        "error_message": str(e),
                    },
                ) from e

        # Should not reach here, but handle just in case
        if last_error:
            raise DatabaseError(
                message=f"Database query failed after {self.max_retries + 1} attempts: {last_error!s}",
                details={"attempts": self.max_retries + 1},
            ) from last_error
        raise DatabaseError(message="Query execution failed unexpectedly")

    async def _execute_once(
        self,
        sql: str,
        timeout: float,  # noqa: ASYNC109
        max_rows: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Execute SQL query once without retry logic.

        Args:
            sql: SQL query to execute.
            timeout: Query timeout in seconds.
            max_rows: Maximum rows to return.

        Returns:
            tuple: (results, total_row_count)
        """
        async with (
            self.pool.acquire() as connection,
            connection.transaction(readonly=True),
        ):
            # Set session parameters for security
            await self._set_session_params(connection, timeout)

            # Execute query with timeout
            try:
                records = await asyncio.wait_for(
                    connection.fetch(sql),
                    timeout=timeout,
                )
            except TimeoutError as e:
                raise ExecutionTimeoutError(
                    message=f"Query execution exceeded timeout of {timeout} seconds",
                    details={
                        "timeout_seconds": timeout,
                        "sql": sql[:200],
                    },
                ) from e

            # Track total count before limiting
            total_count = len(records)

            # Limit number of returned rows
            if len(records) > max_rows:
                records = records[:max_rows]

            # Convert asyncpg.Record to dict
            results = [dict(record) for record in records]

            # Serialize special PostgreSQL types
            results = self._serialize_results(results)

            return results, total_count

    async def _set_session_params(
        self,
        conn: Connection,
        timeout: float,  # noqa: ASYNC109
    ) -> None:
        """Set session parameters to ensure safe query execution.

        This method configures the database session with:
        1. statement_timeout: Prevents long-running queries
        2. search_path: Prevents schema injection attacks
        3. SET ROLE: Switches to read-only role if configured

        Args:
            conn: Database connection to configure.
            timeout: Query timeout in seconds (converted to milliseconds).

        Raises:
            DatabaseError: If setting session parameters fails.

        Note:
            These settings apply only to the current transaction and are
            automatically reset when the connection is returned to the pool.
        """
        try:
            # Set statement timeout (PostgreSQL expects milliseconds)
            timeout_ms = int(timeout * 1000)
            await conn.execute(f"SET statement_timeout = {timeout_ms}")

            # Set safe search_path to prevent schema injection
            # Using execute with literal to avoid SQL injection
            search_path = self.security_config.safe_search_path
            # Validate search_path contains only safe characters
            if not all(c.isalnum() or c in ("_", ",", " ") for c in search_path):
                raise DatabaseError(
                    message="Invalid search_path configuration",
                    details={"search_path": search_path},
                )
            await conn.execute(f"SET search_path = '{search_path}'")

            # Switch to read-only role if configured
            if self.security_config.readonly_role:
                readonly_role = self.security_config.readonly_role
                # Validate role name contains only safe characters
                if not all(c.isalnum() or c == "_" for c in readonly_role):
                    raise DatabaseError(
                        message="Invalid readonly_role configuration",
                        details={"readonly_role": readonly_role},
                    )
                await conn.execute(f"SET ROLE {readonly_role}")

        except asyncpg.PostgresError as e:
            raise DatabaseError(
                message=f"Failed to set session parameters: {e!s}",
                details={
                    "error_code": e.sqlstate if hasattr(e, "sqlstate") else None,
                    "timeout_ms": timeout_ms,
                    "search_path": self.security_config.safe_search_path,
                    "readonly_role": self.security_config.readonly_role,
                },
            ) from e

    def _serialize_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Serialize PostgreSQL-specific types to JSON-compatible types.

        This method handles serialization of types that are not natively
        JSON-serializable, including:
        - datetime types: converted to ISO format strings
        - decimal.Decimal: converted to float
        - uuid.UUID: converted to string
        - bytes: converted to hexadecimal string
        - Nested lists/dicts: recursively serialized

        Args:
            results: List of row dictionaries with potentially unserializable values.

        Returns:
            list: Results with all values serialized to JSON-compatible types.

        Example:
            >>> results = [
            ...     {"id": 1, "created": datetime.datetime(2024, 1, 1, 12, 0)},
            ...     {"id": 2, "price": decimal.Decimal("99.99")}
            ... ]
            >>> serialized = executor._serialize_results(results)
            >>> serialized[0]["created"]  # "2024-01-01T12:00:00"
            >>> serialized[1]["price"]  # 99.99
        """

        def serialize_value(value: Any) -> Any:
            """Recursively serialize a single value.

            Args:
                value: Value to serialize.

            Returns:
                Serialized value that is JSON-compatible.
            """
            # Handle None
            if value is None:
                return None

            # Handle datetime types
            if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
                return value.isoformat()

            # Handle timedelta
            if isinstance(value, datetime.timedelta):
                return str(value)

            # Handle Decimal (convert to float)
            if isinstance(value, decimal.Decimal):
                return float(value)

            # Handle UUID
            if isinstance(value, uuid.UUID):
                return str(value)

            # Handle bytes (convert to hex string)
            if isinstance(value, bytes):
                return value.hex()

            # Handle lists and tuples (recursively serialize)
            if isinstance(value, (list, tuple)):
                return [serialize_value(v) for v in value]

            # Handle dicts (recursively serialize values)
            if isinstance(value, dict):
                return {k: serialize_value(v) for k, v in value.items()}

            # Return other types as-is (str, int, float, bool, etc.)
            return value

        # Serialize all values in all rows
        return [{key: serialize_value(value) for key, value in row.items()} for row in results]
