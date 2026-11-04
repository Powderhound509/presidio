"""ASE-hosted SQL outbox dispatcher and supervised Storage Queue consumer."""

import hashlib
import json
import logging
import multiprocessing
import os
import signal
import tempfile
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config
from .contract import (
    MAX_BYTES,
    TERMINAL,
    JobError,
    Submission,
    opaque,
    parse_message,
    queue_message,
)
from .sql import Sql

LOG = logging.getLogger("document_worker")


def _child(config_values, row, pipe, scratch):
    """Keep parsers/conversion/HTTP deadlines within a killable private process."""
    from azure.core import MatchConditions
    from azure.core.exceptions import AzureError
    from azure.storage.blob import BlobServiceClient, ContentSettings

    import ase_document_tools as tools

    from .formats import process
    from .ocr import AzureRead

    credential = None
    blob = None
    try:
        if os.name != "nt":
            os.setsid()
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
            resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
        config = Config(**config_values)
        submission = Submission(
            row["document_id"],
            row["version_id"],
            row["extension"],
            row["input_etag"],
            row["operation"],
            row["language"],
            row["policy"],
        )
        credential = config.credential()
        blob = BlobServiceClient(
            config.blob_endpoint,
            credential=credential,
            retry_total=3,
            connection_timeout=10,
            read_timeout=30,
            logging_enable=False,
        )
        source_blob = blob.get_blob_client(config.input_container, submission.blob)
        properties = source_blob.get_blob_properties()
        if properties.size > MAX_BYTES or properties.etag != submission.etag:
            raise JobError("input_version_changed_or_oversized")
        # All temporary paths and blob names are opaque; original names are never supplied.
        with tempfile.TemporaryDirectory(prefix="processing-", dir=scratch) as job_scratch:
            root = Path(job_scratch)
            source = root / ("input." + submission.extension)
            output = root / "output"
            output.mkdir()
            with source.open("xb") as stream:
                download = source_blob.download_blob(
                    etag=submission.etag,
                    match_condition=MatchConditions.IfNotModified,
                    max_concurrency=1,
                )
                count = 0
                for chunk in download.chunks():
                    count += len(chunk)
                    if count > MAX_BYTES:
                        raise JobError("input_size_limit")
                    stream.write(chunk)
            with tools.ASEAnalyzer(
                config.analyzer_endpoint,
                config.analyzer_resource,
                config.client_id if config.identity == "user" else "",
                submission.language,
            ) as analyzer:
                read = AzureRead(config, credential)
                try:
                    manifest = process(source, output, submission.operation, analyzer.analyze, read)
                finally:
                    read.session.close()
            prefix = opaque(row["job_id"]) + "/" + opaque(row["claim_token"]) + "/"
            for artifact in manifest["artifacts"]:
                path = output / artifact["name"]
                if path.stat().st_size > MAX_BYTES:
                    raise JobError("output_size_limit")
                artifact["bytes"] = path.stat().st_size
                with path.open("rb") as stream:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                    artifact["sha256"] = digest.hexdigest()
                with path.open("rb") as stream:
                    target = blob.get_blob_client(
                        config.output_container, prefix + artifact["name"]
                    )
                    target.upload_blob(
                        stream,
                        overwrite=False,
                        max_concurrency=1,
                        content_settings=ContentSettings(content_type=artifact["content_type"]),
                    )
            # Upload the complete manifest last. It is not authorized until SQL Complete wins.
            payload = json.dumps(manifest).encode("utf-8")
            blob.get_blob_client(config.output_container, prefix + "manifest.json").upload_blob(
                payload,
                overwrite=False,
                content_settings=ContentSettings(content_type="application/json"),
            )
            pipe.send({"ok": True, "warnings": bool(manifest["warnings"])})
    except JobError as exc:
        pipe.send({"ok": False, "code": exc.code, "retryable": exc.retryable})
    except AzureError as exc:
        # Status codes, not exception messages/bodies, decide whether to retry.
        status = getattr(exc, "status_code", None)
        pipe.send(
            {
                "ok": False,
                "code": "storage_failed",
                "retryable": status in (None, 408, 429, 500, 502, 503, 504),
            }
        )
    except (ValueError, RuntimeError, OSError):
        pipe.send({"ok": False, "code": "document_processing_failed", "retryable": False})
    except Exception:
        # Last process boundary: parser bugs must become explicit failed jobs, without PII traces.
        pipe.send({"ok": False, "code": "unexpected_processing_failure", "retryable": False})
    finally:
        if blob is not None:
            blob.close()
        if credential is not None:
            credential.close()
        pipe.close()


class Worker:
    """Single bounded consumer; scale existing approved ASE replicas for concurrency."""

    def __init__(self, config, sql, queue, stop):
        self.config, self.sql, self.queue, self.stop = config, sql, queue, stop
        self.last_progress = time.monotonic()

    def dispatch(self):
        """Publish opaque IDs, then conditionally mark the leased outbox row sent."""
        self.sql.call("Reconcile")
        rows = self.sql.call("Dispatch", self.config.lease_seconds)
        if rows:
            row = rows[0]
            self.queue.send_message(queue_message(row["job_id"]), time_to_live=-1)
            self.sql.call("Dispatched", row["job_id"], row["dispatch_token"])

    def consume(self, message):
        """Claim in SQL before processing; renew both independent leases."""
        try:
            job_id = parse_message(message.content)
        except JobError:
            # Malformed messages cannot reference an accepted SQL job. Delete after safe diagnosis.
            LOG.error("invalid_queue_message")
            self.queue.delete_message(message.id, message.pop_receipt)
            return
        rows = self.sql.call("Claim", job_id, self.config.lease_seconds, self.config.max_attempts)
        if not rows:
            LOG.error("queue_job_missing")
            self.queue.delete_message(message.id, message.pop_receipt)
            return
        row = rows[0]
        if row["status"] in TERMINAL:
            self.queue.delete_message(message.id, message.pop_receipt)
            return
        if not row["claimed"]:
            return
        outcome, receipt = self.run_claim(row, message)
        if outcome is None:
            # Ownership/visibility was lost; never complete or acknowledge from this worker.
            return
        if outcome["ok"]:
            result = self.sql.call("Complete", job_id, row["claim_token"], outcome["warnings"])
            if result and result[0]["owned"]:
                self.queue.delete_message(message.id, receipt)
        else:
            result = self.sql.call(
                "Fail",
                job_id,
                row["claim_token"],
                outcome["code"],
                outcome["retryable"],
                self.config.max_attempts,
            )
            if result and result[0]["status"] in TERMINAL:
                self.queue.delete_message(message.id, receipt)

    def run_claim(self, row, message):
        """Supervise a subprocess with a hard wall deadline and expiring SQL fencing."""
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        scratch = tempfile.TemporaryDirectory(prefix="document-job-")
        process = context.Process(
            target=_child, args=(asdict(self.config), row, child, scratch.name)
        )
        process.start()
        child.close()
        start = renewed = time.monotonic()
        receipt = message.pop_receipt
        try:
            while process.is_alive() and not self.stop.is_set():
                self.last_progress = time.monotonic()
                if time.monotonic() - start >= self.config.job_timeout_seconds:
                    return {"ok": False, "code": "job_deadline", "retryable": True}, receipt
                if time.monotonic() - renewed >= self.config.lease_seconds / 3:
                    rows = self.sql.call(
                        "Renew", row["job_id"], row["claim_token"], self.config.lease_seconds
                    )
                    if not rows or not rows[0]["owned"]:
                        LOG.error("sql_claim_lost")
                        return None, receipt
                    updated = self.queue.update_message(
                        message.id,
                        pop_receipt=receipt,
                        visibility_timeout=self.config.lease_seconds,
                    )
                    receipt = updated.pop_receipt
                    renewed = time.monotonic()
                process.join(timeout=1)
            if self.stop.is_set():
                return None, receipt
            if parent.poll():
                try:
                    return parent.recv(), receipt
                except EOFError:
                    # A crashed child can close the pipe without reporting an outcome.
                    pass
            return {"ok": False, "code": "processing_process_exited", "retryable": True}, receipt
        finally:
            if os.name != "nt":
                try:
                    # The child creates its own process group, including LibreOffice.
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
                if process.is_alive():
                    process.kill()
                    process.join()
            parent.close()
            process.close()
            scratch.cleanup()

    def run(self):
        """Keep loop errors visible and let SQL/Queue expiry recover abandoned work."""
        from azure.core.exceptions import AzureError

        while not self.stop.is_set():
            try:
                if self.config.processing_enabled:
                    self.dispatch()
                    messages = self.queue.receive_messages(
                        messages_per_page=1, visibility_timeout=self.config.lease_seconds
                    )
                    for message in messages:
                        self.consume(message)
                        break
                self.last_progress = time.monotonic()
            except JobError as exc:
                LOG.error("%s", exc.code)
            except AzureError:
                LOG.error("queue_operation_failed")
            self.stop.wait(self.config.poll_seconds)


def main():
    """Start an explicitly configured worker and health endpoint in an existing ASE app."""
    from azure.storage.queue import QueueClient

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # SDK/Pipeline informational diagnostics are not part of this solution's safe log schema.
    logging.getLogger("azure").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)
    try:
        config = Config.load(Path(os.environ["DOCUMENT_WORKER_CONFIG"]))
    except (KeyError, JobError):
        LOG.error("invalid_configuration")
        raise SystemExit(2) from None
    stop = threading.Event()
    credential = config.credential()
    queue = QueueClient(
        config.queue_endpoint,
        config.queue_name,
        credential=credential,
        connection_timeout=10,
        read_timeout=30,
        retry_total=3,
        logging_enable=False,
    )
    worker = Worker(config, Sql(config, credential), queue, stop)

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            healthy = worker.last_progress > time.monotonic() - config.lease_seconds
            if self.path not in ("/health", "/ready"):
                self.send_response(404)
            else:
                ready = healthy and (self.path == "/health" or config.processing_enabled)
                self.send_response(200 if ready else 503)
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        worker.run()
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join()
        queue.close()
        credential.close()


if __name__ == "__main__":
    main()
