"""Exercise the ASE notebook with synthetic PDFs and mocked authentication/HTTP."""

import contextlib
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTChar, LTTextContainer
from pikepdf import Array, Dictionary, Name, Pdf


NOTEBOOK = Path(__file__).resolve().parents[1] / "example_pdf_annotation.ipynb"
EMAILS = ("alpha@example.test", "beta@example.test")


class PdfAnnotationTests(unittest.TestCase):
    """Validate the actual notebook cells without contacting Azure."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.input_pdf = Path(self.temporary.name) / "input.pdf"
        self.output_pdf = Path(self.temporary.name) / "output.pdf"
        self.cells = json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]
        self.namespace = {}
        self.console = io.StringIO()
        self.http = Mock(spec=requests.Session)
        self.http.request.side_effect = self.response
        self.credential = Mock()
        self.credential.get_token.return_value = SimpleNamespace(
            token="synthetic-test-token"
        )
        self.create_pdf()

    def create_pdf(self):
        with Pdf.new() as pdf:
            font = pdf.make_indirect(
                Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)
            )
            for email in EMAILS:
                page = pdf.add_blank_page(page_size=(612, 792))
                page.Resources = Dictionary(Font=Dictionary(F1=font))
                content = (
                    "BT /F1 12 Tf 72 720 Td (Account) Tj "
                    f"0 -14 Td ({email}) Tj ET"
                )
                page.Contents = pdf.make_stream(content.encode("ascii"))
            pdf.pages[0].Annots = Array(
                [
                    pdf.make_indirect(
                        Dictionary(
                            Type=Name.Annot,
                            Subtype=Name.Text,
                            Rect=[10, 10, 20, 20],
                            Contents="Existing synthetic annotation",
                        )
                    )
                ]
            )
            pdf.save(self.input_pdf)

    @staticmethod
    def response(method, url, **kwargs):
        response = Mock(status_code=200)
        if method == "POST":
            text = kwargs["json"]["text"]
            detections = []
            for email in EMAILS:
                if email in text:
                    start = text.index(email)
                    detections.append(
                        {
                            "start": start,
                            "end": start + len(email),
                            "score": 0.85,
                            "entity_type": "EMAIL_ADDRESS",
                        }
                    )
            response.json.return_value = detections
        return response

    def run_notebook(self):
        environment = {
            "INPUT_PDF": str(self.input_pdf),
            "OUTPUT_PDF": str(self.output_pdf),
            "ANALYZER_BASE_URL": "https://analyzer.example.test",
            "ANALYZER_API_RESOURCE": "api://synthetic-api",
            "MANAGED_IDENTITY_CLIENT_ID": "synthetic-client-id",
        }
        with (
            patch.dict("os.environ", environment),
            patch("requests.Session", return_value=self.http),
            patch(
                "azure.identity.ManagedIdentityCredential",
                return_value=self.credential,
            ),
            contextlib.redirect_stdout(self.console),
        ):
            for cell in self.cells:
                if cell["cell_type"] == "code":
                    source = "".join(cell["source"])
                    exec(compile(source, cell["id"], "exec"), self.namespace)

    def test_when_two_pages_then_highlights_stay_on_original_pages(self):
        self.run_notebook()

        with Pdf.open(self.output_pdf) as pdf:
            self.assertEqual(len(pdf.pages), 2)
            self.assertEqual(len(pdf.pages[0].Annots), 2)
            self.assertEqual(len(pdf.pages[1].Annots), 1)
            self.assertEqual(
                str(pdf.pages[0].Annots[0].Contents), "Existing synthetic annotation"
            )
            for index, page_layout in enumerate(extract_pages(self.input_pdf)):
                expected_glyphs = []
                for container in page_layout:
                    if not isinstance(container, LTTextContainer):
                        continue
                    for line in container:
                        if EMAILS[index] in line.get_text():
                            expected_glyphs.extend(
                                item for item in line if isinstance(item, LTChar)
                            )
                expected_box = (
                    min(item.x0 for item in expected_glyphs),
                    min(item.y0 for item in expected_glyphs),
                    max(item.x1 for item in expected_glyphs),
                    max(item.y1 for item in expected_glyphs),
                )
                highlight = pdf.pages[index].Annots[-1]
                self.assertEqual(highlight.Subtype, Name.Highlight)
                self.assertEqual(str(highlight.T), "EMAIL_ADDRESS")
                self.assertEqual(
                    tuple(round(float(value), 6) for value in highlight.Rect),
                    tuple(round(value, 6) for value in expected_box),
                )

        post_calls = [
            call for call in self.http.request.call_args_list if call.args[0] == "POST"
        ]
        self.assertEqual(len(post_calls), 2)
        self.assertEqual(
            [call.kwargs["json"]["text"] for call in post_calls],
            [f"Account\n{email}\n" for email in EMAILS],
        )
        for call in self.http.request.call_args_list:
            self.assertTrue(call.kwargs["verify"])
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["timeout"], (10, 60))
            self.assertEqual(
                call.kwargs["headers"],
                {"Authorization": "Bearer synthetic-test-token"},
            )
        self.credential.get_token.assert_called_with("api://synthetic-api/.default")
        self.credential.close.assert_called_once()
        self.http.close.assert_called_once()
        for sensitive in (*EMAILS, "synthetic-test-token", "Account"):
            self.assertNotIn(sensitive, self.console.getvalue())

    def test_when_detection_spans_lines_then_each_line_has_its_own_highlight(self):
        def multiline_response(method, url, **kwargs):
            response = self.response(method, url, **kwargs)
            if method == "POST":
                response.json.return_value = [
                    {
                        "start": 0,
                        "end": len(kwargs["json"]["text"]) - 1,
                        "score": 0.85,
                        "entity_type": "CUSTOM_ENTITY",
                    }
                ]
            return response

        self.http.request.side_effect = multiline_response
        self.run_notebook()
        with Pdf.open(self.output_pdf) as pdf:
            self.assertEqual(len(pdf.pages[0].Annots), 3)
            self.assertEqual(len(pdf.pages[1].Annots), 2)
            self.assertNotEqual(
                list(pdf.pages[1].Annots[0].Rect),
                list(pdf.pages[1].Annots[1].Rect),
            )

    def test_when_no_detections_then_existing_annotations_are_preserved(self):
        self.http.request.return_value = Mock(status_code=200)
        self.http.request.side_effect = None
        self.http.request.return_value.json.return_value = []
        self.run_notebook()
        with Pdf.open(self.output_pdf) as pdf:
            self.assertEqual(len(pdf.pages[0].Annots), 1)
            self.assertNotIn(Name.Annots, pdf.pages[1])
        self.assertIn("0 detections", self.console.getvalue())

    def test_when_http_forbidden_then_error_omits_response_body_and_token(self):
        self.http.request.side_effect = None
        self.http.request.return_value = Mock(
            status_code=403, text="synthetic-private-error-body"
        )
        with self.assertRaisesRegex(RuntimeError, "HTTP 403") as error:
            self.run_notebook()
        self.assertNotIn("synthetic-private-error-body", str(error.exception))
        self.assertNotIn("synthetic-test-token", str(error.exception))
        self.assertFalse(self.output_pdf.exists())

    def test_when_invalid_json_then_error_omits_original_exception(self):
        def invalid_response(method, url, **kwargs):
            response = self.response(method, url, **kwargs)
            if method == "POST":
                response.json.side_effect = ValueError("synthetic-private-body")
            return response

        self.http.request.side_effect = invalid_response
        with self.assertRaisesRegex(RuntimeError, "invalid JSON") as error:
            self.run_notebook()
        self.assertNotIn("synthetic-private-body", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)
        self.assertFalse(self.output_pdf.exists())

    def test_when_invalid_detection_then_validation_rejects_it(self):
        self.run_notebook()
        validate = self.namespace["validate_detections"]
        valid = {"start": 0, "end": 3, "score": 0.85, "entity_type": "PERSON"}
        self.assertEqual(validate([valid], "abc"), [valid])
        self.assertEqual(validate([], "abc"), [])
        for change in (
            {"start": -1},
            {"start": True},
            {"end": 4},
            {"end": 0},
            {"score": math.nan},
            {"score": math.inf},
            {"score": True},
            {"score": 1.01},
            {"entity_type": ""},
        ):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                validate([valid | change], "abc")
        for data in ({}, [None], ["not a detection"]):
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                validate(data, "abc")

    def test_when_output_exists_then_rerun_does_not_overwrite_it(self):
        self.run_notebook()
        original = self.output_pdf.read_bytes()
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            self.namespace["validate_configuration"]()
        self.assertEqual(self.output_pdf.read_bytes(), original)
        self.namespace["OUTPUT_PDF"] = self.input_pdf
        with self.assertRaisesRegex(ValueError, "different files"):
            self.namespace["validate_configuration"]()

    def test_when_no_extractable_text_then_no_output_is_saved(self):
        with Pdf.new() as pdf:
            pdf.add_blank_page()
            pdf.save(self.input_pdf)
        with self.assertRaisesRegex(RuntimeError, "No extractable text"):
            self.run_notebook()
        self.assertFalse(self.output_pdf.exists())
        self.assertEqual(self.http.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
