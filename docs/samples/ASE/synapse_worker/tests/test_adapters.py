"""Transport boundaries use synthetic responses and never contact live services."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parent))

from presidio_document_worker.config import Config
from presidio_document_worker.contract import JobError
from presidio_document_worker.ocr import AzureRead
from presidio_document_worker.sql import Sql


def available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


@unittest.skipUnless(
    all(available(name) for name in ("requests", "azure.core", "PIL")),
    "Azure Core, requests and Pillow are required",
)
class ReadTransportTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image

        self.config = Config(**json.loads((ROOT / "config.example.json").read_text()))
        self.image = Image.new("RGB", (100, 50), "white")
        self.credential = Mock()
        self.credential.get_token.return_value = SimpleNamespace(token="synthetic-access-token")
        self.session = Mock()
        self.read = AzureRead(self.config, self.credential, self.session)
        self.operation = (
            self.config.ocr_endpoint
            + "/formrecognizer/documentModels/prebuilt-read/analyzeResults/opaque"
            + "?api-version=2023-07-31"
        )

    def response(self, status, body=None, headers=None):
        response = Mock(status_code=status, headers=headers or {})
        response.json.return_value = body
        return response

    def succeeded(self):
        return self.response(
            200,
            {
                "status": "succeeded",
                "analyzeResult": {
                    "content": "Mary",
                    "pages": [
                        {
                            "pageNumber": 1,
                            "unit": "pixel",
                            "width": 100,
                            "height": 50,
                            "words": [
                                {
                                    "content": "Mary",
                                    "confidence": 0.9,
                                    "span": {"offset": 0, "length": 4},
                                    "polygon": [10, 10, 90, 10, 90, 40, 10, 40],
                                }
                            ],
                        }
                    ],
                },
            },
        )

    def test_when_read_succeeds_then_send_private_bytes_with_codepoint_indexing(self):
        self.session.request.side_effect = [
            self.response(202, headers={"Operation-Location": self.operation}),
            self.succeeded(),
        ]
        with patch("presidio_document_worker.ocr.time.sleep"):
            text, geometry = self.read(self.image)
        self.assertEqual(text, "Mary")
        self.assertEqual(geometry, [(10, 10, 90, 40)] * 4)
        post, poll = self.session.request.call_args_list
        self.assertEqual(post.args[0], "POST")
        self.assertIn("stringIndexType=unicodeCodePoint", post.args[1])
        self.assertTrue(post.kwargs["data"].startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(poll.args, ("GET", self.operation))
        for request in (post, poll):
            self.assertFalse(request.kwargs["allow_redirects"])
            self.assertTrue(request.kwargs["verify"])
            self.assertEqual(
                request.kwargs["headers"]["Authorization"], "Bearer synthetic-access-token"
            )
        self.assertEqual(self.credential.get_token.call_count, 2)

    def test_when_operation_location_changes_host_then_do_not_forward_authentication(self):
        self.session.request.return_value = self.response(
            202, headers={"Operation-Location": "https://unapproved.example/analyzeResults/opaque"}
        )
        with self.assertRaisesRegex(JobError, "invalid_ocr_operation"):
            self.read(self.image)
        self.assertEqual(self.session.request.call_count, 1)

    def test_when_service_throttles_then_retry_is_bounded_and_failure_is_explicit(self):
        self.session.request.return_value = self.response(429, headers={"Retry-After": "1"})
        with patch("presidio_document_worker.ocr.time.sleep"):
            with self.assertRaisesRegex(JobError, "ocr_throttled"):
                self.read(self.image)
        self.assertEqual(self.session.request.call_count, 4)

    def test_when_identity_or_transport_fails_then_never_propagate_sensitive_exceptions(self):
        import requests
        from azure.core.exceptions import ClientAuthenticationError

        for error, expected in (
            (ClientAuthenticationError("sensitive source"), "ocr_identity_failed"),
            (requests.ConnectionError("sensitive source"), "ocr_transport_failed"),
        ):
            self.credential.get_token.side_effect = (
                error if isinstance(error, ClientAuthenticationError) else None
            )
            self.session.request.side_effect = (
                error if isinstance(error, requests.ConnectionError) else None
            )
            with self.subTest(expected=expected), self.assertRaises(JobError) as caught:
                self.read(self.image)
            self.assertEqual(str(caught.exception), expected)
            self.assertNotIn("sensitive", str(caught.exception))


@unittest.skipUnless(
    available("pyodbc") and available("azure.core"),
    "pyodbc and Azure Core are required",
)
class SqlAdapterTests(unittest.TestCase):
    def test_when_calling_sql_then_use_connection_timeout_and_bound_parameters(self):
        class Cursor:
            __slots__ = ("description", "arguments")

            def __init__(self):
                self.description = [("job_id",)]
                self.arguments = None

            def execute(self, *arguments):
                self.arguments = arguments

            def fetchall(self):
                return [("00000000-0000-0000-0000-000000000003",)]

        class Connection:
            __slots__ = ("timeout", "closed", "result")

            def __init__(self):
                self.timeout = 0
                self.closed = False
                self.result = Cursor()

            def cursor(self):
                return self.result

            def close(self):
                self.closed = True

        config = SimpleNamespace(
            sql_server="approved.database.usgovcloudapi.net", sql_database="documents"
        )
        credential = Mock()
        credential.get_token.return_value = SimpleNamespace(token="synthetic-access-token")
        connection = Connection()
        job_id = "00000000-0000-0000-0000-000000000003"
        with patch("pyodbc.connect", return_value=connection) as connect:
            result = Sql(config, credential).call("Status", job_id)
        self.assertEqual(result, [{"job_id": job_id}])
        self.assertEqual(connection.timeout, 30)
        self.assertEqual(connection.result.arguments, ("EXEC docjobs.Status ?", job_id))
        self.assertTrue(connection.closed)
        self.assertIn("Encrypt=yes;TrustServerCertificate=no", connect.call_args.args[0])
        self.assertNotIn("synthetic-access-token", connect.call_args.args[0])
        self.assertNotIn("Password", connect.call_args.args[0])

    def test_when_caller_requests_arbitrary_sql_then_reject_before_opening_connection(self):
        sql = Sql(Mock(), Mock())
        with patch.object(sql, "connection") as connect:
            with self.assertRaisesRegex(JobError, "invalid_sql_operation"):
                sql.call("Status;DROP TABLE docjobs.Jobs", "untrusted")
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
