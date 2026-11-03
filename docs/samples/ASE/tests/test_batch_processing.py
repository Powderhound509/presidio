"""Test the ASE batch notebook with mocked REST services and synthetic data."""

import ast
import contextlib
import copy
import io
import json
import math
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit


NOTEBOOK = Path(__file__).resolve().parents[1] / "presidio_batch_processing.ipynb"


class IdentityError(Exception):
    """Synthetic identity failure."""


class RequestError(Exception):
    """Synthetic HTTP transport failure."""


class TlsError(RequestError):
    """Synthetic TLS failure."""


class TimeoutError(RequestError):
    """Synthetic timeout."""


class BatchNotebookTests(unittest.TestCase):
    """Exercise the notebook's actual functions, payloads, and output structures."""

    def setUp(self):
        self.notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        self.calls = []
        self.credential = Mock()
        self.credential.get_token.return_value = SimpleNamespace(
            token="synthetic-token"
        )
        self.session = Mock()
        self.session.request.side_effect = self.http_response
        self.namespace = {
            "copy": copy,
            "math": math,
            "urlsplit": urlsplit,
            "LANGUAGE": "en",
            "BATCH_SIZE": 1,
            "TIMEOUT": (10, 60),
            "ANONYMIZERS": {
                "DEFAULT": {"type": "replace", "new_value": "[REDACTED]"}
            },
            "SERVICES": {
                name: {
                    "url": f"https://{name}.example.test",
                    "resource": f"api://{name}-resource",
                }
                for name in ("analyzer", "anonymizer")
            },
            "credential": self.credential,
            "session": self.session,
            "AzureError": IdentityError,
            "requests": SimpleNamespace(
                exceptions=SimpleNamespace(
                    SSLError=TlsError,
                    Timeout=TimeoutError,
                    RequestException=RequestError,
                )
            ),
        }
        nodes = []
        for cell in self.notebook["cells"]:
            if cell["cell_type"] == "code":
                tree = ast.parse("".join(cell["source"]), filename=cell["id"])
                nodes.extend(
                    node for node in tree.body if isinstance(node, ast.FunctionDef)
                )
        future = ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0
        )
        tree = ast.fix_missing_locations(
            ast.Module(body=[future, *nodes], type_ignores=[])
        )
        exec(compile(tree, str(NOTEBOOK), "exec"), self.namespace)

    @staticmethod
    def detect(text):
        if "secret" not in text:
            return []
        start = text.index("secret")
        return [
            {
                "start": start,
                "end": start + 6,
                "score": 0.85,
                "entity_type": "TEST_ENTITY",
                "analysis_explanation": {"unused": "not forwarded"},
            }
        ]

    def http_response(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        payload = kwargs["json"]
        if url.endswith("/health"):
            data = {}
        elif url.endswith("/analyze"):
            texts = payload["text"]
            data = (
                [self.detect(text) for text in texts]
                if isinstance(texts, list)
                else self.detect(texts)
            )
        else:
            self.assertTrue(url.endswith("/anonymize"))
            text = payload["text"]
            result = payload["analyzer_results"][0]
            self.assertNotIn("analysis_explanation", result)
            self.assertEqual(text[result["start"]:result["end"]], "secret")
            data = {
                "text": (
                    text[:result["start"]] + "[REDACTED]" + text[result["end"]:]
                )
            }
        return SimpleNamespace(status_code=200, json=lambda: data)

    def test_when_notebook_is_loaded_then_all_cells_compile_and_outputs_are_clear(self):
        self.assertEqual(self.notebook["nbformat"], 4)
        ids = [cell["id"] for cell in self.notebook["cells"]]
        self.assertEqual(len(ids), len(set(ids)))
        for cell in self.notebook["cells"]:
            if cell["cell_type"] == "code":
                source = "".join(cell["source"])
                compile(source, cell["id"], "exec")
                self.assertIsNone(cell["execution_count"])
                self.assertEqual(cell["outputs"], [])
                self.assertNotIn("presidio_analyzer", source)
                self.assertNotIn("presidio_anonymizer", source)
                self.assertNotIn("multiprocessing", source)

    def test_when_scalar_mode_then_original_text_offsets_and_service_scopes_match(self):
        output = self.namespace["anonymize_values"](["é secret\n", "clean"])
        self.assertEqual(output, ["é [REDACTED]\n", "clean"])
        self.assertEqual([call[2]["json"]["text"] for call in self.calls], [
            "é secret\n", "é secret\n", "clean"
        ])
        scopes = [call.args[0] for call in self.credential.get_token.call_args_list]
        self.assertEqual(scopes, [
            "api://analyzer-resource/.default",
            "api://anonymizer-resource/.default",
            "api://analyzer-resource/.default",
        ])
        for method, url, kwargs in self.calls:
            self.assertEqual(method, "POST")
            self.assertTrue(url.startswith("https://"))
            self.assertEqual(kwargs["timeout"], (10, 60))
            self.assertTrue(kwargs["verify"])
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(
                kwargs["headers"], {"Authorization": "Bearer synthetic-token"}
            )

    def test_when_batch_mode_then_chunks_and_short_last_batch_preserve_order(self):
        self.namespace["BATCH_SIZE"] = 2
        values = ["secret one", "clean", "two secret", "secret three", "secret four"]
        self.assertEqual(
            self.namespace["anonymize_values"](values),
            ["[REDACTED] one", "clean", "two [REDACTED]",
             "[REDACTED] three", "[REDACTED] four"],
        )
        analyzer_payloads = [
            call[2]["json"]["text"] for call in self.calls
            if call[1].endswith("/analyze")
        ]
        self.assertEqual(analyzer_payloads, [
            ["secret one", "clean"], ["two secret", "secret three"], ["secret four"]
        ])

    def test_when_values_are_non_strings_or_empty_then_types_and_values_are_preserved(self):
        self.namespace["BATCH_SIZE"] = 3
        values = [1, True, None, 0, False, 2.5, "", "secret"]
        output = self.namespace["anonymize_values"](values)
        self.assertEqual(output, [1, True, None, 0, False, 2.5, "", "[REDACTED]"])
        self.assertEqual(
            [type(value) for value in output],
            [type(value) for value in values],
        )
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.calls[0][2]["json"]["text"], ["1", "True"])
        self.assertEqual(self.calls[1][2]["json"]["text"], ["0", "False", "2.5"])
        self.assertEqual(self.calls[2][2]["json"]["text"], ["secret"])

    def test_when_numeric_value_is_detected_then_it_is_not_left_as_unprocessed_pii(self):
        def numeric_response(method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            payload = kwargs["json"]
            self.assertEqual(payload["text"], "123456789")
            if url.endswith("/analyze"):
                data = [{"start": 0, "end": 9, "score": 0.85, "entity_type": "TEST_ID"}]
            else:
                self.assertTrue(url.endswith("/anonymize"))
                data = {"text": "[REDACTED]"}
            return SimpleNamespace(status_code=200, json=lambda: data)

        self.session.request.side_effect = numeric_response
        self.assertEqual(
            self.namespace["anonymize_data"]({"identifier": 123456789}),
            {"identifier": "[REDACTED]"},
        )
        self.assertEqual(len(self.calls), 2)

    def test_when_nested_lists_and_dicts_then_shape_and_input_are_preserved(self):
        value = {
            "outer": {"email": "secret"},
            "contacts": [{"email": "secret"}, {"email": "clean"}],
            "mixed": [["secret", None], 3, {"name": "secret"}],
            "empty": {},
        }
        original = copy.deepcopy(value)
        output = self.namespace["anonymize_data"](value)
        self.assertEqual(output, {
            "outer": {"email": "[REDACTED]"},
            "contacts": [{"email": "[REDACTED]"}, {"email": "clean"}],
            "mixed": [["[REDACTED]", None], 3, {"name": "[REDACTED]"}],
            "empty": {},
        })
        self.assertEqual(value, original)
        self.assertEqual(self.calls[0][2]["json"]["context"], ["outer", "email"])

    def test_when_bare_keys_are_skipped_then_all_matching_subtrees_remain_unchanged(self):
        value = {"outer": {"name": ["secret"]}, "name": "secret", "other": "secret"}
        output = self.namespace["anonymize_data"](value, keys_to_skip=["name"])
        self.assertEqual(output, {
            "outer": {"name": ["secret"]}, "name": "secret", "other": "[REDACTED]"
        })
        output["outer"]["name"].append("new")
        self.assertEqual(value["outer"]["name"], ["secret"])
        self.assertEqual(len(self.calls), 2)

    def test_when_dotted_path_is_skipped_then_similarly_prefixed_paths_are_processed(self):
        value = {
            "key_a": {"key_a1": "secret"},
            "key_ab": {"key_a1": "secret"},
            "contacts": [{"email": "secret", "name": "secret"}],
        }
        output = self.namespace["anonymize_data"](
            value, keys_to_skip=["key_a.key_a1", "contacts.email"]
        )
        self.assertEqual(output, {
            "key_a": {"key_a1": "secret"},
            "key_ab": {"key_a1": "[REDACTED]"},
            "contacts": [{"email": "secret", "name": "[REDACTED]"}],
        })

    def test_when_dictionary_represents_table_then_column_lengths_and_order_are_preserved(self):
        value = {
            "names": ["secret", "clean", "secret"],
            "integer": [1, 2, 3],
            "boolean": [True, False, False],
        }
        output = self.namespace["anonymize_data"](value)
        self.assertEqual(list(output), ["names", "integer", "boolean"])
        self.assertEqual(output, {
            "names": ["[REDACTED]", "clean", "[REDACTED]"],
            "integer": [1, 2, 3], "boolean": [True, False, False],
        })

    def test_when_batch_response_has_wrong_count_then_no_anonymizer_call_is_made(self):
        self.namespace["BATCH_SIZE"] = 2
        self.session.request.side_effect = None
        self.session.request.return_value = SimpleNamespace(
            status_code=200, json=lambda: [self.detect("secret")]
        )
        with self.assertRaisesRegex(RuntimeError, "count does not match"):
            self.namespace["anonymize_values"](["secret", "secret"])
        self.assertEqual(self.session.request.call_count, 1)

    def test_when_detection_is_invalid_then_it_is_rejected_before_anonymization(self):
        validate = self.namespace["validate_detections"]
        valid = {"start": 0, "end": 6, "score": 0.85, "entity_type": "TEST"}
        for change in (
            {"start": True}, {"start": -1}, {"end": 7}, {"end": 0},
            {"score": math.nan}, {"score": math.inf}, {"score": True},
            {"score": 1.1}, {"entity_type": ""},
        ):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                validate([valid | change], "secret")
        for data in ({}, [None], [["nested incorrectly"]]):
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                validate(data, "secret")

    def test_when_inputs_or_skip_specs_are_invalid_then_no_api_calls_are_made(self):
        for value in ({"set"}, {1: "secret"}, ["secret", object()], math.nan, math.inf):
            with self.subTest(value_type=type(value)), self.assertRaises(ValueError):
                self.namespace["anonymize_data"](value)
        for skips in ("name", [""], ["a..b"], [None]):
            with self.subTest(skips=skips), self.assertRaises(ValueError):
                self.namespace["anonymize_data"]("secret", keys_to_skip=skips)
        self.session.request.assert_not_called()

    def test_when_anonymizer_response_is_malformed_then_no_partial_result_is_returned(self):
        def malformed(method, url, **kwargs):
            response = self.http_response(method, url, **kwargs)
            if url.endswith("/anonymize"):
                return SimpleNamespace(status_code=200, json=lambda: {"text": None})
            return response

        self.session.request.side_effect = malformed
        values = ["secret", "clean"]
        with self.assertRaisesRegex(RuntimeError, "string text field"):
            self.namespace["anonymize_values"](values)
        self.assertEqual(values, ["secret", "clean"])

    def test_when_http_or_json_fails_then_errors_do_not_expose_sensitive_details(self):
        request = self.namespace["api_request"]
        self.session.request.side_effect = None
        self.session.request.return_value = SimpleNamespace(
            status_code=403, text="private-body"
        )
        with self.assertRaisesRegex(RuntimeError, "HTTP 403") as error:
            request("analyzer", "/health")
        self.assertNotIn("private-body", str(error.exception))
        self.assertNotIn("synthetic-token", str(error.exception))
        response = Mock()
        response.json.side_effect = ValueError("private-body")
        with self.assertRaisesRegex(RuntimeError, "invalid JSON") as error:
            self.namespace["response_json"](response, "analyzer")
        self.assertTrue(error.exception.__suppress_context__)

    def test_when_identity_or_transport_fails_then_original_details_are_suppressed(self):
        request = self.namespace["api_request"]
        self.credential.get_token.side_effect = IdentityError("private-details")
        with self.assertRaisesRegex(RuntimeError, "Token acquisition failed") as error:
            request("analyzer", "/health")
        self.assertTrue(error.exception.__suppress_context__)
        self.session.request.assert_not_called()
        self.credential.get_token.side_effect = None
        for failure, message in (
            (TlsError, "TLS validation"), (TimeoutError, "timed out"),
            (RequestError, "request failed"),
        ):
            self.session.request.side_effect = failure("private-details")
            with self.subTest(failure=failure), self.assertRaisesRegex(
                RuntimeError, message
            ) as error:
                request("analyzer", "/health")
            self.assertTrue(error.exception.__suppress_context__)
            self.assertNotIn("private-details", str(error.exception))

    def test_when_configuration_is_invalid_then_validation_fails_before_requests(self):
        validate = self.namespace["validate_services"]
        validate()
        for size in (0, -1, True, 2.5):
            self.namespace["BATCH_SIZE"] = size
            with self.subTest(size=size), self.assertRaisesRegex(ValueError, "BATCH_SIZE"):
                validate()
        self.namespace["BATCH_SIZE"] = 1
        for resource in ("", "api://analyzer/.default"):
            self.namespace["SERVICES"]["analyzer"]["resource"] = resource
            with self.subTest(resource=resource), self.assertRaisesRegex(
                ValueError, "ANALYZER_API_RESOURCE"
            ):
                validate()

    def test_when_processing_data_then_no_source_or_output_text_is_printed(self):
        console = io.StringIO()
        with contextlib.redirect_stdout(console):
            output = self.namespace["anonymize_data"]({"name": "secret"})
        self.assertEqual(output, {"name": "[REDACTED]"})
        self.assertEqual(console.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
