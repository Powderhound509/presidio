"""Short-lived, encrypted SQL sessions; tokens never leave the connection adapter."""

import struct
from contextlib import contextmanager

from .contract import JobError

PROCEDURES = {
    "Submit": 7,
    "Status": 1,
    "Dispatch": 1,
    "Dispatched": 2,
    "Claim": 3,
    "Renew": 3,
    "Complete": 3,
    "Fail": 5,
    "Reconcile": 0,
}


class Sql:
    """Worker SQL adapter with explicit managed identity and narrowly scoped procedures."""

    def __init__(self, config, credential):
        self.config = config
        self.credential = credential

    @contextmanager
    def connection(self):
        """Create a fresh token-authenticated connection, avoiding stale pooled tokens."""
        import pyodbc
        from azure.core.exceptions import AzureError

        pyodbc.pooling = False
        try:
            token = self.credential.get_token("https://database.usgovcloudapi.net/.default")
            encoded = token.token.encode("utf-16-le")
            packed = struct.pack("<I", len(encoded)) + encoded
            connection = pyodbc.connect(
                "Driver={ODBC Driver 18 for SQL Server};"
                f"Server=tcp:{self.config.sql_server},1433;"
                f"Database={self.config.sql_database};"
                "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=15;",
                attrs_before={1256: packed},
                autocommit=True,
            )
        except (AzureError, pyodbc.Error):
            raise JobError("sql_connection_failed", True) from None
        try:
            yield connection
        finally:
            connection.close()

    def call(self, procedure, *values):
        """Call only fixed procedure names with bound parameters and bounded timeouts."""
        import pyodbc

        if procedure not in PROCEDURES or len(values) != PROCEDURES[procedure]:
            raise JobError("invalid_sql_operation")
        try:
            with self.connection() as connection:
                connection.timeout = 30
                cursor = connection.cursor()
                placeholders = ",".join("?" for _ in values)
                cursor.execute(f"EXEC docjobs.{procedure} {placeholders}", *values)
                if cursor.description is None:
                    return []
                columns = [column[0] for column in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except pyodbc.Error:
            raise JobError("sql_operation_failed", True) from None
