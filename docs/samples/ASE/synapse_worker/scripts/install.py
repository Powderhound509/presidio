"""Explicit operator-only data-plane installation; never run by the worker identity."""

import argparse
import json
import re
import struct
from pathlib import Path
from uuid import UUID

from presidio_document_worker.config import Config
from presidio_document_worker.contract import JobError


def install(config, principals, apply):
    """Create only approved objects inside existing services using operator CLI identity."""
    import pyodbc
    from azure.core.exceptions import ResourceExistsError
    from azure.identity import AzureCliCredential
    from azure.storage.blob import BlobServiceClient
    from azure.storage.queue import QueueClient

    # az cloud/account context is verified by the operator before running this script.
    with AzureCliCredential(process_timeout=30) as credential:
        token = credential.get_token("https://database.usgovcloudapi.net/.default")
        encoded = token.token.encode("utf-16-le")
        packed = struct.pack("<I", len(encoded)) + encoded
        with pyodbc.connect(
            "Driver={ODBC Driver 18 for SQL Server};"
            f"Server=tcp:{config.sql_server},1433;Database={config.sql_database};"
            "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=15;",
            attrs_before={1256: packed},
            autocommit=True,
        ) as connection:
            connection.timeout = 60
            cursor = connection.cursor()
            cursor.execute("SELECT 1")
            print("sql_connection_verified")
            if apply:
                migration = Path(__file__).resolve().parents[1] / "sql" / "001_jobs.sql"
                for batch in re.split(r"(?im)^\s*GO\s*$", migration.read_text(encoding="utf-8")):
                    if batch.strip():
                        cursor.execute(batch)
                        while cursor.nextset():
                            pass
                for principal in principals:
                    name = principal["name"]
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name):
                        raise JobError("invalid_sql_principal_name")
                    object_id = str(UUID(principal["object_id"]))
                    role = principal["role"]
                    if role not in ("document_submitter", "document_worker"):
                        raise JobError("invalid_sql_role")
                    # Dynamic DDL uses validated identifiers and UUIDs, never caller job data.
                    cursor.execute(
                        f"DECLARE @sid binary(16) = CONVERT(binary(16),"
                        f"CAST('{object_id}' AS uniqueidentifier));"
                        f"IF DATABASE_PRINCIPAL_ID(N'{name}') IS NULL "
                        f"BEGIN DECLARE @ddl nvarchar(max) = N'CREATE USER [{name}] WITH SID='"
                        f"+ CONVERT(nvarchar(34), @sid, 1) + N', TYPE=E'; EXEC(@ddl); END;"
                        f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals "
                        f"WHERE name=N'{name}' AND sid=@sid) "
                        "THROW 51000,'principal_sid_mismatch',1;"
                        f"IF IS_ROLEMEMBER(N'{role}',N'{name}') <> 1 "
                        f"ALTER ROLE [{role}] ADD MEMBER [{name}];"
                    )
                print("sql_objects_installed")
        with BlobServiceClient(config.blob_endpoint, credential=credential) as service:
            for name in (config.input_container, config.output_container):
                client = service.get_container_client(name)
                if apply:
                    try:
                        client.create_container()
                    except ResourceExistsError:
                        print("container_present")
                client.get_container_properties()
                print("container_verified")
        with QueueClient(config.queue_endpoint, config.queue_name, credential=credential) as queue:
            if apply:
                try:
                    queue.create_queue()
                except ResourceExistsError:
                    print("queue_present")
            queue.get_queue_properties()
            print("queue_verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--principals", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        install(
            Config.load(args.config),
            json.loads(args.principals.read_text(encoding="utf-8")),
            args.apply,
        )
    except Exception:
        # Operator CLI/SQL failures may include identity tokens or server-side input.
        raise SystemExit("installation_failed_check_grants_connectivity_and_schema") from None
