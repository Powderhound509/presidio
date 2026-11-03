"""Dependency-free checks of the functions defined in the ASE PDF notebook."""

import ast
import contextlib
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from urllib.parse import urlsplit


NOTEBOOK = Path(__file__).resolve().parents[1] / "example_pdf_annotation.ipynb"


class Glyph:
    """Minimal glyph fixture for checking character offset alignment."""

    def __init__(self, text, box):
        self.text = text
        self.bbox = box

    def get_text(self):
        return self.text


class Spacer:
    """Text with no corresponding visible glyph."""

    def __init__(self, text):
        self.text = text

    def get_text(self):
        return self.text


class Line(list):
    """Minimal PDF text line."""

    def get_text(self):
        return "".join(item.get_text() for item in self)


class Container(list):
    """Minimal PDF text container."""

    def get_text(self):
        return "".join(line.get_text() for line in self)


class IdentityFailure(Exception):
    """Synthetic identity error."""


class TransportFailure(Exception):
    """Synthetic transport error."""


class TlsFailure(TransportFailure):
    """Synthetic TLS error."""


class TimeoutFailure(TransportFailure):
    """Synthetic timeout."""


class NotebookHelperTests(unittest.TestCase):
    """Execute notebook function definitions without external service dependencies."""

    def setUp(self):
        self.notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        input_path = Path(self.temporary.name) / "input.pdf"
        input_path.write_bytes(b"synthetic path-validation fixture")
        self.credential = Mock()
        self.credential.get_token.return_value = SimpleNamespace(
            token="synthetic-secret-token"
        )
        self.session = Mock()
        self.session.request.return_value = Mock(status_code=200)
        self.namespace = {
            "math": math,
            "urlsplit": urlsplit,
            "LTChar": Glyph,
            "LTTextLine": Line,
            "AzureError": IdentityFailure,
            "ANALYZER_BASE_URL": "https://analyzer.example.test",
            "ANALYZER_API_RESOURCE": "api://synthetic-api",
            "LANGUAGE": "en",
            "INPUT_PDF": input_path,
            "OUTPUT_PDF": input_path.with_name("output.pdf"),
            "TIMEOUT": (10, 60),
            "credential": self.credential,
            "session": self.session,
            "requests": SimpleNamespace(
                exceptions=SimpleNamespace(
                    SSLError=TlsFailure,
                    Timeout=TimeoutFailure,
                    RequestException=TransportFailure,
                )
            ),
        }
        definitions = []
        for cell in self.notebook["cells"]:
            if cell["cell_type"] == "code":
                tree = ast.parse("".join(cell["source"]), filename=cell["id"])
                definitions.extend(
                    node for node in tree.body if isinstance(node, ast.FunctionDef)
                )
        future = ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0
        )
        module = ast.fix_missing_locations(
            ast.Module(body=[future, *definitions], type_ignores=[])
        )
        exec(compile(module, str(NOTEBOOK), "exec"), self.namespace)

    def test_when_notebook_is_loaded_then_all_cells_compile_and_outputs_are_empty(self):
        self.assertEqual(self.notebook["nbformat"], 4)
        ids = [cell["id"] for cell in self.notebook["cells"]]
        self.assertEqual(len(ids), len(set(ids)))
        for cell in self.notebook["cells"]:
            if cell["cell_type"] == "code":
                compile("".join(cell["source"]), cell["id"], "exec")
                self.assertIsNone(cell["execution_count"])
                self.assertEqual(cell["outputs"], [])

    def test_when_spaces_newlines_and_multichar_glyphs_then_offsets_stay_aligned(self):
        first = (0, 0, 1, 1)
        second = (2, 0, 3, 1)
        third = (0, 2, 2, 3)
        container = Container(
            [
                Line([Glyph("A", first), Spacer(" "), Glyph("B", second), Spacer("\n")]),
                Line([Glyph("fi", third), Spacer("\n")]),
            ]
        )
        text, geometry = self.namespace["text_with_geometry"](container)
        self.assertEqual(text, "A B\nfi\n")
        self.assertEqual(
            geometry,
            [(0, first), None, (0, second), None, (1, third), (1, third), None],
        )
        self.assertEqual(
            self.namespace["detection_boxes"]({"start": 2, "end": 6}, geometry),
            [second, third],
        )

    def test_when_detection_spans_same_line_then_boxes_are_combined_exactly(self):
        geometry = [(0, (1, 2, 3, 4)), None, (0, (4, 1, 6, 5))]
        self.assertEqual(
            self.namespace["detection_boxes"]({"start": 0, "end": 3}, geometry),
            [(1, 1, 6, 5)],
        )

    def test_when_detection_has_no_glyphs_then_error_is_explicit(self):
        with self.assertRaisesRegex(RuntimeError, "no visible PDF glyphs"):
            self.namespace["detection_boxes"]({"start": 0, "end": 1}, [None])

    def test_when_extraction_disagrees_then_annotation_stops(self):
        container = Container([Line([Glyph("A", (0, 0, 1, 1))])])
        container.get_text = lambda: "different"
        with self.assertRaisesRegex(RuntimeError, "do not align"):
            self.namespace["text_with_geometry"](container)
        with self.assertRaisesRegex(RuntimeError, "Unsupported PDF text layout"):
            self.namespace["text_with_geometry"](Container([Spacer("unsupported")]))

    def test_when_rest_detection_is_valid_then_exact_boundaries_are_accepted(self):
        result = {"start": 0, "end": 3, "score": 0.85, "entity_type": "PERSON"}
        validate = self.namespace["validate_detections"]
        self.assertEqual(validate([result], "abc"), [result])
        self.assertEqual(validate([], "abc"), [])

    def test_when_rest_detection_is_invalid_then_it_is_rejected(self):
        result = {"start": 0, "end": 3, "score": 0.85, "entity_type": "PERSON"}
        validate = self.namespace["validate_detections"]
        for change in (
            {"start": -1},
            {"start": True},
            {"end": 4},
            {"end": 0},
            {"score": math.inf},
            {"score": math.nan},
            {"score": True},
            {"score": -0.1},
            {"score": 1.1},
            {"entity_type": ""},
            {"entity_type": " "},
        ):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                validate([result | change], "abc")
        for data in ({}, ["invalid"], [None]):
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                validate(data, "abc")

    def test_when_request_is_sent_then_token_scope_and_transport_are_correct(self):
        request = self.namespace["analyzer_request"]
        request("/health")
        request("/analyze", {"text": "synthetic", "language": "en"})
        self.credential.get_token.assert_called_with("api://synthetic-api/.default")
        self.assertEqual(self.session.request.call_args.args, (
            "POST", "https://analyzer.example.test/analyze"
        ))
        self.assertEqual(
            self.session.request.call_args.kwargs,
            {
                "headers": {"Authorization": "Bearer synthetic-secret-token"},
                "json": {"text": "synthetic", "language": "en"},
                "timeout": (10, 60),
                "allow_redirects": False,
                "verify": True,
            },
        )
        self.assertEqual(self.session.request.call_args_list[0].args[0], "GET")

    def test_when_http_rejects_request_then_error_does_not_expose_body_or_token(self):
        for status in (302, 401, 403, 413, 500):
            self.session.request.return_value = Mock(
                status_code=status, text="synthetic-private-response"
            )
            with self.subTest(status=status), self.assertRaisesRegex(
                RuntimeError, f"HTTP {status}"
            ) as error:
                self.namespace["analyzer_request"]("/health")
            self.assertNotIn("synthetic-private-response", str(error.exception))
            self.assertNotIn("synthetic-secret-token", str(error.exception))

    def test_when_transport_fails_then_private_exception_details_are_suppressed(self):
        for failure, message in (
            (TlsFailure, "TLS validation failed"),
            (TimeoutFailure, "timed out"),
            (TransportFailure, "request failed"),
        ):
            self.session.request.side_effect = failure("synthetic-private-details")
            with self.subTest(failure=failure), self.assertRaisesRegex(
                RuntimeError, message
            ) as error:
                self.namespace["analyzer_request"]("/health")
            self.assertTrue(error.exception.__suppress_context__)
            self.assertNotIn("synthetic-private-details", str(error.exception))

    def test_when_identity_fails_then_no_http_request_is_sent(self):
        self.credential.get_token.side_effect = IdentityFailure("private-details")
        with self.assertRaisesRegex(RuntimeError, "Token acquisition failed") as error:
            self.namespace["analyzer_request"]("/health")
        self.assertTrue(error.exception.__suppress_context__)
        self.session.request.assert_not_called()

    def test_when_configuration_is_invalid_then_actionable_error_is_raised(self):
        validate = self.namespace["validate_configuration"]
        validate()
        for url in (
            "http://analyzer.example.test",
            "https://user:password@analyzer.example.test",
            "https://analyzer.example.test/path",
            "https://analyzer.example.test?query=1",
            "https://analyzer.example.test:80",
            "https://analyzer.example.test:invalid",
        ):
            self.namespace["ANALYZER_BASE_URL"] = url
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "HTTPS"):
                validate()
        self.namespace["ANALYZER_BASE_URL"] = "https://analyzer.example.test"
        for resource in ("", "api://synthetic-api/.default"):
            self.namespace["ANALYZER_API_RESOURCE"] = resource
            with self.subTest(resource=resource), self.assertRaisesRegex(
                ValueError, "ANALYZER_API_RESOURCE"
            ):
                validate()

    def test_when_paths_are_unsafe_or_missing_then_no_file_is_overwritten(self):
        validate = self.namespace["validate_configuration"]
        input_path = self.namespace["INPUT_PDF"]
        output_path = self.namespace["OUTPUT_PDF"]
        self.namespace["OUTPUT_PDF"] = input_path
        with self.assertRaisesRegex(ValueError, "different files"):
            validate()
        self.namespace["OUTPUT_PDF"] = output_path
        output_path.write_bytes(b"existing output")
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            validate()
        self.assertEqual(output_path.read_bytes(), b"existing output")
        self.namespace["OUTPUT_PDF"] = output_path.parent / "missing" / "output.pdf"
        with self.assertRaisesRegex(FileNotFoundError, "parent folder"):
            validate()
        self.namespace["OUTPUT_PDF"] = output_path.with_name("new.pdf")
        self.namespace["INPUT_PDF"] = input_path.with_name("missing.pdf")
        with self.assertRaisesRegex(FileNotFoundError, "INPUT_PDF"):
            validate()

    def test_when_pipeline_runs_then_page_mapping_and_existing_annotations_survive(self):
        boxes = ((10, 20, 30, 40), (50, 60, 70, 80))
        layouts = [
            [Container([Line([Glyph("lead", (0, 0, 1, 1)), Spacer("\n")]),
                        Line([Glyph("synthetic", box), Spacer("\n")])])]
            for box in boxes
        ]

        def response(method, url, **kwargs):
            self.assertEqual(kwargs["json"]["text"], "lead\nsynthetic\n")
            return SimpleNamespace(
                status_code=200,
                json=lambda: [
                    {"start": 5, "end": 14, "score": 0.85, "entity_type": "PERSON"}
                ],
            )

        self.session.request.side_effect = response
        pages = [MagicMock(), MagicMock()]
        pages[0].__contains__.return_value = True
        pages[0].Annots = ["existing annotation"]
        pages[1].__contains__.return_value = False
        pdf = MagicMock()
        pdf.pages = pages
        pdf.make_indirect.side_effect = lambda value: value
        pdf_type = MagicMock()
        pdf_type.open.return_value.__enter__.return_value = pdf
        self.namespace.update(
            LTTextContainer=Container,
            extract_pages=lambda path: layouts,
            Pdf=pdf_type,
            Array=list,
            Dictionary=lambda **kwargs: SimpleNamespace(**kwargs),
            Name=SimpleNamespace(Annot="Annot", Highlight="Highlight", Annots="Annots"),
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for cell in self.notebook["cells"]:
                if cell["id"] in ("analyze", "annotate"):
                    exec(
                        compile("".join(cell["source"]), cell["id"], "exec"),
                        self.namespace,
                    )

        self.assertEqual(self.namespace["page_count"], 2)
        self.assertEqual(self.namespace["detection_count"], 2)
        self.assertEqual(len(pages[0].Annots), 2)
        self.assertEqual(pages[0].Annots[0], "existing annotation")
        self.assertEqual(len(pages[1].Annots), 1)
        for page, box in zip(pages, boxes):
            annotation = page.Annots[-1]
            self.assertEqual(annotation.Rect, list(box))
            self.assertEqual(annotation.Subtype, "Highlight")
            self.assertEqual(annotation.T, "PERSON")
        pdf.save.assert_called_once_with(self.namespace["OUTPUT_PDF"])
        self.assertNotIn("lead\nsynthetic", output.getvalue())

    def test_when_analyzer_returns_invalid_json_then_processing_stops_privately(self):
        self.namespace.update(
            LTTextContainer=Container,
            extract_pages=lambda path: [
                [Container([Line([Glyph("synthetic", (0, 0, 1, 1))])])]
            ],
        )
        self.session.request.return_value.json.side_effect = ValueError(
            "synthetic-private-response"
        )
        cell = next(cell for cell in self.notebook["cells"] if cell["id"] == "analyze")
        with self.assertRaisesRegex(RuntimeError, "invalid JSON") as error:
            exec(compile("".join(cell["source"]), cell["id"], "exec"), self.namespace)
        self.assertTrue(error.exception.__suppress_context__)
        self.assertNotIn("synthetic-private-response", str(error.exception))
        self.assertFalse(self.namespace["OUTPUT_PDF"].exists())


if __name__ == "__main__":
    unittest.main()
