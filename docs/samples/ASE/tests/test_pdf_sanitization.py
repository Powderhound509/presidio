"""Synthetic output-level tests for searchable labeled PDF sanitization."""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SAMPLES = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sanitization_tools", SAMPLES / "ase_document_tools.py"
)
TOOLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOLS)
VALUES = {
    "alex@example.com": "EMAIL_ADDRESS",
    "321-54-6789": "US_SSN",
    "212-555-0198": "PHONE_NUMBER",
    "Alex Example": "PERSON",
    "Seattle": "LOCATION",
    "4111 1111 1111 1111": "CREDIT_CARD",
}


def detect(text):
    results = []
    for value, entity in VALUES.items():
        offset = 0
        while value in text[offset:]:
            start = text.index(value, offset)
            results.append(
                {
                    "start": start,
                    "end": start + len(value),
                    "score": 0.9,
                    "entity_type": entity,
                }
            )
            offset = start + len(value)
    return results


class RegionTests(unittest.TestCase):
    def setUp(self):
        self.image = SimpleNamespace(width=1000, height=1000)

    def test_when_multiline_match_then_unrelated_space_between_lines_is_not_removed(
        self,
    ):
        text = "abc\ndef"
        geometry = [(10, 10, 40, 30)] * 3 + [None] + [(10, 70, 40, 90)] * 3
        regions = TOOLS._labeled_regions(
            text,
            geometry,
            [
                {"start": 0, "end": 7, "score": 0.9, "entity_type": "PERSON"},
            ],
            self.image,
        )
        self.assertEqual(
            regions,
            [
                ((8, 8, 42, 32), {"PERSON"}),
                ((8, 68, 42, 92), {"PERSON"}),
            ],
        )

    def test_when_regions_overlap_then_all_types_are_retained(
        self,
    ):
        regions = [
            ((10, 10, 60, 30), {"EMAIL_ADDRESS"}),
            ((10, 10, 60, 30), {"EMAIL_ADDRESS"}),
            ((50, 10, 90, 30), {"PERSON"}),
            ((80, 10, 100, 30), {"LOCATION"}),
        ]
        self.assertEqual(
            TOOLS._merge_labeled_regions(regions),
            [
                ((10, 10, 100, 30), {"EMAIL_ADDRESS", "PERSON", "LOCATION"}),
            ],
        )

    def test_when_only_part_of_word_is_removed_then_entire_word_is_excluded_from_search(
        self,
    ):
        geometry = [(10, 10, 90, 30)] * 8 + [None] + [(110, 10, 150, 30)] * 4
        self.assertEqual(
            list(
                TOOLS._searchable_words(
                    "prefixPI safe",
                    geometry,
                    [((70, 8, 92, 32), {"TEST"})],
                    self.image,
                )
            ),
            [("safe", (110, 10, 150, 30))],
        )

    def test_when_geometry_is_invalid_then_processing_fails_explicitly(self):
        for box in ((-1, 0, 3, 5), (1, 2, 1, 4), (0, 0, float("nan"), 4)):
            with (
                self.subTest(box=box),
                self.assertRaisesRegex(RuntimeError, "geometry"),
            ):
                TOOLS._checked_box(box, self.image)
        with self.assertRaisesRegex(RuntimeError, "align"):
            list(TOOLS._searchable_words("word", [], [], self.image))
        with self.assertRaisesRegex(RuntimeError, "missing geometry"):
            list(TOOLS._searchable_words("word", [None] * 4, [], self.image))

    def test_when_detection_has_invalid_offsets_or_entity_then_processing_fails(self):
        for detection in (
            {"start": 0, "end": 8, "score": 0.9, "entity_type": "TEST"},
            {"start": 0, "end": 4, "score": 0.9, "entity_type": "unsafe label"},
        ):
            with self.subTest(detection=detection), self.assertRaises(RuntimeError):
                TOOLS._labeled_regions(
                    "word", [(1, 1, 40, 20)] * 4, [detection], self.image
                )

    def test_when_notebook_is_loaded_then_cells_are_valid_and_outputs_are_empty(self):
        notebook = json.loads((SAMPLES / "presidio_pdf_sanitization.ipynb").read_text())
        self.assertEqual((notebook["nbformat"], notebook["nbformat_minor"]), (4, 5))
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                compile("".join(cell["source"]), cell["id"], "exec")
                self.assertIsNone(cell["execution_count"])
                self.assertEqual(cell["outputs"], [])


@unittest.skipUnless(
    importlib.util.find_spec("PIL") and importlib.util.find_spec("pymupdf"),
    "Install Pillow and PyMuPDF for PDF output-level validation.",
)
class PDFSanitizationTests(unittest.TestCase):
    def setUp(self):
        import pymupdf

        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.source = Path(self.folder.name) / "input.pdf"
        self.target = Path(self.folder.name) / "sanitized.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=600, height=800)
            page.insert_text((40, 40), "Public heading", fontsize=14)
            for index, value in enumerate(VALUES):
                page.insert_text((40, 100 + 70 * index), value, fontsize=18)
            page.insert_text((40, 650), "alex@example.com", fontsize=18)
            pdf.set_metadata({"author": "private-owner", "subject": "321-54-6789"})
            pdf.embfile_add("private-attachment", b"private-body")
            pdf.save(self.source)
        self.original = self.source.read_bytes()

    def ocr(self, image, **kwargs):
        import pymupdf

        # Native geometry supplies deterministic OCR fixtures, not a second detector.
        with pymupdf.open(self.source) as pdf:
            blocks = list(TOOLS._pdf_native_text(pdf[0], image))
        parts, geometry = [], []
        for text, boxes in blocks:
            if parts:
                parts.append("\n")
                geometry.append(None)
            parts.append(text)
            geometry.extend(boxes)
        return "".join(parts), geometry

    def test_when_multiple_entities_then_labels_replace_pixels_and_search_text(
        self,
    ):
        import pymupdf
        from PIL import Image

        with patch.object(TOOLS, "_ocr", side_effect=self.ocr):
            summary = TOOLS.sanitize_pdf(self.source, self.target, detect, dpi=144)
        self.assertEqual(
            summary,
            {
                "pages": 1,
                "text_detections": 7,
                "ocr_detections": 7,
                "label_types": {
                    entity: 2 if entity == "EMAIL_ADDRESS" else 1
                    for entity in VALUES.values()
                },
            },
        )
        with pymupdf.open(self.target) as pdf:
            text = pdf[0].get_text()
            for value, entity in VALUES.items():
                self.assertNotIn(value, text)
                self.assertIn(f"<{entity}>", text)
            self.assertIn("Public", text)
            self.assertIn("heading", text)
            self.assertEqual(text.count("<EMAIL_ADDRESS>"), 2)
            self.assertEqual(pdf[0].rect, pymupdf.Rect(0, 0, 600, 800))
            self.assertEqual(pdf.embfile_count(), 0)
            self.assertEqual(pdf.metadata["author"], "")
            self.assertEqual(pdf.metadata["subject"], "")
            self.assertEqual(list(pdf[0].annots()), [])
            # Inspect the underlying page image separately from the visible labels.
            image_data = pdf.extract_image(pdf[0].get_images()[0][0])["image"]
            with Image.open(io.BytesIO(image_data)) as image:
                for y in (100, 170, 240, 310, 380, 450, 650):
                    self.assertEqual(
                        image.crop((80, y * 2 - 20, 180, y * 2)).getextrema(),
                        ((255, 255), (255, 255), (255, 255)),
                    )
            rendered = pdf[0].get_pixmap(dpi=144)
            rendered_image = Image.frombytes(
                "RGB", (rendered.width, rendered.height), rendered.samples
            )
            self.assertNotEqual(
                rendered_image.crop((80, 160, 370, 220)).getextrema(),
                ((255, 255), (255, 255), (255, 255)),
            )
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_when_no_detections_then_non_pii_text_remains_searchable(self):
        import pymupdf

        with patch.object(TOOLS, "_ocr", side_effect=self.ocr):
            summary = TOOLS.sanitize_pdf(
                self.source, self.target, lambda text: [], dpi=144
            )
        self.assertEqual(summary["label_types"], {})
        with pymupdf.open(self.target) as pdf:
            self.assertIn("alex@example.com", pdf[0].get_text())

    def test_when_pdf_has_scanned_and_native_pages_then_both_are_sanitized(self):
        import pymupdf

        mixed = self.source.with_name("mixed.pdf")
        with pymupdf.open(self.source) as original, pymupdf.open() as output:
            output.insert_pdf(original)
            page = output.new_page(width=600, height=800)
            page.insert_image(page.rect, pixmap=original[0].get_pixmap(dpi=144))
            output.save(mixed)
        with patch.object(TOOLS, "_ocr", side_effect=self.ocr):
            summary = TOOLS.sanitize_pdf(mixed, self.target, detect, dpi=144)
        self.assertEqual(summary["pages"], 2)
        self.assertEqual(summary["text_detections"], 7)
        self.assertEqual(summary["ocr_detections"], 14)
        with pymupdf.open(self.target) as pdf:
            for page in pdf:
                self.assertIn("Public", page.get_text())
                for value, entity in VALUES.items():
                    self.assertNotIn(value, page.get_text())
                    self.assertIn(f"<{entity}>", page.get_text())

    def test_when_detections_overlap_then_combined_labels_are_exact_and_unique(self):
        import pymupdf

        def overlapping(text):
            results = detect(text)
            return results + [
                {**result, "entity_type": "PERSON"}
                for result in results
                if result["entity_type"] == "EMAIL_ADDRESS"
            ]

        with patch.object(TOOLS, "_ocr", side_effect=self.ocr):
            TOOLS.sanitize_pdf(self.source, self.target, overlapping, dpi=144)
        with pymupdf.open(self.target) as pdf:
            text = pdf[0].get_text()
            self.assertEqual(text.count("<EMAIL_ADDRESS> <PERSON>"), 2)
            self.assertNotIn("alex@example.com", text)

    def test_when_multiline_person_then_each_line_has_a_complete_label(self):
        import pymupdf

        source = self.source.with_name("multiline.pdf")
        with pymupdf.open() as pdf:
            pdf.new_page(width=300, height=200).insert_text(
                (40, 60),
                "Alex\nExample",
                fontsize=18,
            )
            pdf.save(source)

        def multiline(text):
            return (
                [
                    {
                        "start": 0,
                        "end": len(text.rstrip()),
                        "score": 0.9,
                        "entity_type": "PERSON",
                    },
                ]
                if "Alex\nExample" in text
                else []
            )

        with patch.object(TOOLS, "_ocr", return_value=("", [])):
            TOOLS.sanitize_pdf(source, self.target, multiline, dpi=144)
        with pymupdf.open(self.target) as pdf:
            text = pdf[0].get_text()
            self.assertEqual(text.count("<PERSON>"), 2)
            self.assertNotIn("Alex", text)
            self.assertNotIn("Example", text)

    def test_when_partial_word_is_detected_then_ocr_does_not_restore_the_token(self):
        import pymupdf

        source = self.source.with_name("partial.pdf")
        with pymupdf.open() as pdf:
            pdf.new_page(width=600, height=200).insert_text(
                (40, 80),
                "prefixalex@example.comsuffix",
                fontsize=18,
            )
            pdf.save(source)
        self.source = source
        with patch.object(TOOLS, "_ocr", side_effect=self.ocr):
            TOOLS.sanitize_pdf(source, self.target, detect, dpi=144)
        with pymupdf.open(self.target) as pdf:
            self.assertEqual(pdf[0].get_text().strip(), "<EMAIL_ADDRESS>")

    def test_when_rotated_page_then_labels_remain_searchable_and_page_size_is_preserved(
        self,
    ):
        import pymupdf

        with pymupdf.open(self.source) as pdf:
            pdf[0].set_rotation(90)
            pdf.save(self.source.with_name("rotated.pdf"))
        self.source = self.source.with_name("rotated.pdf")
        with patch.object(TOOLS, "_ocr", side_effect=self.ocr):
            TOOLS.sanitize_pdf(self.source, self.target, detect, dpi=144)
        with pymupdf.open(self.target) as pdf:
            self.assertEqual(pdf[0].rect, pymupdf.Rect(0, 0, 800, 600))
            self.assertIn("<EMAIL_ADDRESS>", pdf[0].get_text())
            self.assertNotIn("alex@example.com", pdf[0].get_text())

    def test_when_label_cannot_fit_or_analyzer_fails_then_no_output_is_published(self):
        def wide_label(text):
            results = detect(text)
            for result in results:
                result["entity_type"] = (
                    "EXTREMELY_LONG_ENTITY_LABEL_THAT_CANNOT_FIT_IN_THIS_REGION"
                )
            return results

        for analyzer in (
            wide_label,
            unittest.mock.Mock(side_effect=RuntimeError("Service unavailable")),
        ):
            with (
                self.subTest(analyzer=analyzer),
                patch.object(TOOLS, "_ocr", side_effect=self.ocr),
            ):
                with self.assertRaises(RuntimeError):
                    TOOLS.sanitize_pdf(self.source, self.target, analyzer, dpi=144)
            self.assertFalse(self.target.exists())
            self.assertEqual(self.source.read_bytes(), self.original)

    def test_when_output_exists_or_input_is_output_then_files_are_not_overwritten(self):
        self.target.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            TOOLS.sanitize_pdf(self.source, self.target, detect)
        with self.assertRaises(FileExistsError):
            TOOLS.sanitize_pdf(self.source, self.source, detect)
        self.assertEqual(self.target.read_bytes(), b"existing")
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_when_short_pii_cannot_fit_then_reflow_keeps_complete_labels(self):
        import pymupdf

        source = self.source.with_name("short-name.pdf")
        with pymupdf.open() as pdf:
            pdf.new_page(width=300, height=200).insert_text(
                (40, 60),
                "Hello Al welcome",
                fontsize=10,
            )
            pdf.save(source)
        self.source = source

        def short_name(text):
            start = text.find("Al")
            return (
                [
                    {
                        "start": start,
                        "end": start + 2,
                        "score": 0.9,
                        "entity_type": entity,
                    }
                    for entity in ("PERSON", "LOCATION")
                ]
                if start >= 0
                else []
            )

        with patch.object(TOOLS, "_ocr", side_effect=self.ocr):
            with self.assertRaisesRegex(RuntimeError, "fit legibly"):
                TOOLS.sanitize_pdf(source, self.target, short_name, dpi=144)
            summary = TOOLS.sanitize_pdf(
                source,
                self.target,
                short_name,
                dpi=144,
                layout="reflow",
            )
        self.assertEqual(summary["label_types"], {"LOCATION": 1, "PERSON": 1})
        with pymupdf.open(self.target) as pdf:
            self.assertEqual(
                pdf[0].get_text().strip(), "Hello <LOCATION> <PERSON> welcome"
            )
            self.assertEqual(pdf[0].get_images(), [])
            self.assertEqual(pdf[0].rect, pymupdf.paper_rect("a4"))

    def test_when_reflow_text_overflows_then_new_pages_preserve_every_word(self):
        import pymupdf

        text = "\n".join(["Public"] * 60)
        geometry = [None if char == "\n" else (20, 20, 100, 50) for char in text]
        with patch.object(TOOLS, "_ocr", return_value=(text, geometry)):
            summary = TOOLS.sanitize_pdf(
                self.source,
                self.target,
                lambda text: [],
                dpi=144,
                layout="reflow",
            )
        self.assertEqual(summary["pages"], 2)
        with pymupdf.open(self.target) as pdf:
            self.assertEqual(len(pdf), 2)
            self.assertEqual(sum(page.get_text().count("Public") for page in pdf), 60)
            self.assertEqual(pdf.embfile_count(), 0)
            self.assertEqual(pdf.metadata["author"], "")
            self.assertTrue(all(not page.get_images() for page in pdf))

    def test_when_reflow_has_native_only_detections_then_labels_are_not_lost(self):
        import pymupdf

        with patch.object(TOOLS, "_ocr", return_value=("", [])):
            summary = TOOLS.sanitize_pdf(
                self.source,
                self.target,
                detect,
                dpi=144,
                layout="reflow",
            )
        self.assertEqual(summary["ocr_detections"], 0)
        self.assertEqual(summary["text_detections"], 7)
        with pymupdf.open(self.target) as pdf:
            text = pdf[0].get_text()
            for value, entity in VALUES.items():
                self.assertNotIn(value, text)
                self.assertIn(f"<{entity}>", text)
            self.assertEqual(text.count("<EMAIL_ADDRESS>"), 2)

    def test_when_reflow_label_is_long_then_it_remains_complete_and_unclipped(self):
        import pymupdf

        entity = "W" * 64

        def long_label(text):
            return [{**result, "entity_type": entity} for result in detect(text)]

        with patch.object(TOOLS, "_ocr", side_effect=self.ocr):
            TOOLS.sanitize_pdf(
                self.source,
                self.target,
                long_label,
                dpi=144,
                layout="reflow",
            )
        with pymupdf.open(self.target) as pdf:
            text = "".join(page.get_text() for page in pdf)
            self.assertEqual(text.count(f"<{entity}>"), 7)
            for page in pdf:
                for word in page.get_text("words"):
                    self.assertTrue(page.rect.contains(pymupdf.Rect(word[:4])))

    def test_when_layout_is_invalid_then_processing_fails_before_ocr(self):
        with patch.object(TOOLS, "_ocr") as ocr:
            with self.assertRaisesRegex(ValueError, "layout"):
                TOOLS.sanitize_pdf(self.source, self.target, detect, layout="invalid")
        ocr.assert_not_called()
        self.assertFalse(self.target.exists())

    def test_when_ocr_fails_then_output_is_refused(self):
        for message in ("Install Tesseract", "OCR returned low-confidence text"):
            with (
                self.subTest(message=message),
                patch.object(TOOLS, "_ocr", side_effect=RuntimeError(message)),
            ):
                with self.assertRaisesRegex(RuntimeError, message):
                    TOOLS.sanitize_pdf(self.source, self.target, detect)
                self.assertFalse(self.target.exists())

    @unittest.skipUnless(
        shutil.which("tesseract"), "Real Tesseract executable is required."
    )
    def test_when_notebook_runs_then_synthetic_and_document_paths_produce_labels(self):
        client = MagicMock()
        client.__enter__.return_value.analyze = detect
        notebook = json.loads((SAMPLES / "presidio_pdf_sanitization.ipynb").read_text())
        namespace = {}
        with (
            patch.dict(
                os.environ,
                {
                    "ASE_SAMPLE_DIRECTORY": str(SAMPLES),
                    "INPUT_DOCUMENT": str(self.source),
                    "OUTPUT_DOCUMENT": str(self.target),
                    "TESSERACT_COMMAND": shutil.which("tesseract"),
                },
            ),
            patch.dict(sys.modules, {"ase_document_tools": TOOLS}),
            patch.object(TOOLS, "ASEAnalyzer", return_value=client),
            patch.object(sys, "path", list(sys.path)),
            redirect_stdout(io.StringIO()),
        ):
            for cell in notebook["cells"]:
                if cell["cell_type"] == "code":
                    source = "".join(cell["source"])
                    if cell["id"] == "synthetic-test":
                        source = source.replace(
                            "RUN_SYNTHETIC_TEST = False",
                            "RUN_SYNTHETIC_TEST = True",
                        )
                    exec(compile(source, cell["id"], "exec"), namespace)
        self.assertEqual(namespace["summary"]["pages"], 1)
        self.assertEqual(set(namespace["summary"]["label_types"]), set(VALUES.values()))
        self.assertTrue(self.target.is_file())

    @unittest.skipUnless(
        shutil.which("tesseract"), "Real Tesseract executable is required."
    )
    def test_when_real_ocr_reads_scan_then_output_has_labels_and_searchable_text(
        self,
    ):
        import pymupdf

        scan = self.source.with_name("scan.pdf")
        with pymupdf.open(self.source) as original, pymupdf.open() as output:
            image = original[0].get_pixmap(dpi=200)
            page = output.new_page(width=600, height=800)
            page.insert_image(page.rect, pixmap=image)
            output.save(scan)
        for layout in ("preserve", "reflow"):
            with self.subTest(layout=layout):
                target = self.target.with_name(f"{layout}.pdf")
                summary = TOOLS.sanitize_pdf(
                    scan,
                    target,
                    detect,
                    min_confidence=0,
                    layout=layout,
                )
                self.assertEqual(summary["pages"], 1)
                self.assertEqual(summary["text_detections"], 0)
                with pymupdf.open(target) as pdf:
                    self.assertNotIn("alex@example.com", pdf[0].get_text())
                    self.assertIn("<EMAIL_ADDRESS>", pdf[0].get_text())
                    self.assertIn("Public", pdf[0].get_text())


if __name__ == "__main__":
    unittest.main()
