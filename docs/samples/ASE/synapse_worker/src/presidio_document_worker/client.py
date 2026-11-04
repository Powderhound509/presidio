"""Small Synapse client: only SQL submission/status; no ASE, Queue, or OCR calls."""

import re
import time

from .contract import TERMINAL, JobError, Submission, opaque


class Client:
    """Orchestrator-neutral client accepting a procedure executor.

    :param execute: Callable accepting a fixed procedure name and bound values.
    """

    def __init__(self, execute):
        self.execute = execute

    def submit(self, submission: Submission):
        """Return the durable job ID, not a claim that processing has completed."""
        rows = self.execute(
            "Submit",
            submission.document_id,
            submission.version_id,
            submission.extension,
            submission.etag,
            submission.operation,
            submission.language,
            submission.policy,
        )
        if len(rows) != 1:
            raise JobError("submission_failed")
        return opaque(rows[0]["job_id"])

    def status(self, job_id):
        """Fetch only jobs belonging to the authenticated SQL principal."""
        rows = self.execute("Status", opaque(job_id))
        if len(rows) != 1:
            raise JobError("job_not_authorized_or_missing")
        return rows[0]

    def wait(self, job_id, timeout=3600, interval=15):
        """Bound polling and fail processing batches on failed/dead-letter results."""
        if not 1 <= timeout <= 86400 or not 1 <= interval <= 60:
            raise JobError("invalid_wait_policy")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status(job_id)
            if status["status"] in TERMINAL:
                if status["status"] in ("failed", "dead_letter"):
                    raise JobError("document_processing_failed")
                return status
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        raise JobError("job_wait_timeout")


def synapse_client(spark, sql_server, database, token_library):
    """Use the selected Synapse execution identity and the runtime JDBC driver.

    :param spark: Existing Synapse Spark session.
    :param sql_server: Approved Government SQL hostname.
    :param database: Existing database name.
    :param token_library: Runtime mssparkutils.credentials implementation.
    :return: Client with token acquisition entirely inside its SQL adapter.
    :raises JobError: Invalid configuration or JDBC/identity failure.
    """
    if not re.fullmatch(r"[a-z0-9-]+\.database\.usgovcloudapi\.net", sql_server):
        raise JobError("invalid_sql_server")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", database):
        raise JobError("invalid_sql_database")

    def execute(procedure, *values):
        from .sql import PROCEDURES

        if procedure not in ("Submit", "Status") or len(values) != PROCEDURES[procedure]:
            raise JobError("invalid_sql_operation")
        connection = statement = result = None
        try:
            jvm = spark.sparkContext._gateway.jvm
            properties = jvm.java.util.Properties()
            properties.setProperty("accessToken", token_library.getToken("DW"))
            url = (
                f"jdbc:sqlserver://{sql_server}:1433;databaseName={database};"
                "encrypt=true;trustServerCertificate=false;loginTimeout=15"
            )
            connection = jvm.com.microsoft.sqlserver.jdbc.SQLServerDriver().connect(url, properties)
            marks = ",".join("?" for _ in values)
            statement = connection.prepareStatement(f"EXEC docjobs.{procedure} {marks}")
            statement.setQueryTimeout(30)
            for index, value in enumerate(values, 1):
                statement.setString(index, str(value))
            result = statement.executeQuery()
            meta = result.getMetaData()
            columns = [meta.getColumnLabel(i) for i in range(1, meta.getColumnCount() + 1)]
            rows = []
            while result.next():
                rows.append({name: result.getString(i) for i, name in enumerate(columns, 1)})
            return rows
        except Exception:
            # Py4J errors can embed SQL/JDBC properties; do not propagate raw exceptions.
            raise JobError("synapse_sql_failed") from None
        finally:
            for resource in (result, statement, connection):
                if resource is not None:
                    resource.close()

    return Client(execute)


def configure_storage(spark, dfs_host, linked_service):
    """Select the approved workspace-SAMI ADLS linked service, never SAS or keys.

    :param spark: Existing Synapse Spark session.
    :param dfs_host: Approved Government DFS hostname.
    :param linked_service: Existing ADLS linked service configured with workspace SAMI.
    :raises JobError: Invalid non-secret storage configuration.
    """
    if not re.fullmatch(r"[a-z0-9]+\.dfs\.core\.usgovcloudapi\.net", dfs_host):
        raise JobError("invalid_government_storage")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,140}", linked_service):
        raise JobError("invalid_linked_service")
    spark.conf.set(f"spark.storage.synapse.{dfs_host}.linkedServiceName", linked_service)
    configuration = spark.sparkContext._jsc.hadoopConfiguration()
    configuration.set(f"fs.azure.account.auth.type.{dfs_host}", "OAuth")
    configuration.set(
        f"fs.azure.account.oauth.provider.type.{dfs_host}",
        "com.microsoft.azure.synapse.tokenlibrary.LinkedServiceBasedTokenProvider",
    )
