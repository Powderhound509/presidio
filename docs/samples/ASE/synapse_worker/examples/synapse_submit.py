# Azure Synapse notebook source
# Upload the dependency-free wheel as an approved workspace/pool package.
# Configure workspace-SAMI execution before unattended runs; never fall back to user login.
from notebookutils import mssparkutils
from pyspark.sql import SparkSession

from presidio_document_worker.client import synapse_client
from presidio_document_worker.contract import Submission

# PARAMETERS CELL: non-secret, opaque references to an already uploaded immutable version.
SQL_SERVER = "approvedsql.database.usgovcloudapi.net"
SQL_DATABASE = "documents"
DOCUMENT_ID = "00000000-0000-0000-0000-000000000003"
VERSION_ID = "00000000-0000-0000-0000-000000000004"
INPUT_ETAG = '"0x000000000000001"'
EXTENSION = "pdf"
OPERATION = "redact"
LANGUAGE = "en"

# Reuse the Synapse session; no direct ASE, Queue or OCR connection is required.
spark = SparkSession.builder.getOrCreate()
client = synapse_client(spark, SQL_SERVER, SQL_DATABASE, mssparkutils.credentials)
job_id = client.submit(
    Submission(
        DOCUMENT_ID,
        VERSION_ID,
        EXTENSION,
        INPUT_ETAG,
        OPERATION,
        LANGUAGE,
    )
)
print({"job_id": job_id, "status": "submitted"})

# A production batch must wait for processing, not just SQL acceptance.
result = client.wait(job_id, timeout=3600, interval=15)
print({"job_id": job_id, "status": result["status"]})
# Read the authorized result manifest through the approved ADLS linked-service path.
# Do not display document text, original filenames, annotation contents, or tokens.
