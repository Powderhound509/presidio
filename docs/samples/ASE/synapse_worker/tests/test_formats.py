"""Native libraries are mandatory for release; skips identify missing local dependencies."""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parent))

from presidio_document_worker.contract import JobError
from presidio_document_worker.entities import Labels, Unit, detect
from presidio_document_worker.formats import paint, process, xlsx_preflight


def detect_value(text):
    results = []
    for value, entity in (("Mary", "PERSON"), ("123456789", "US_SSN")):
        start = 0
        while value in text[start:]:
            offset = text.index(value, start)
            results.append(
                {"start": offset, "end": offset + len(value), "entity_type": entity, "score": 0.9}
            )
            start = offset + len(value)
    return results


def fixed_ocr(image):
    return "Mary", [(10, 10, 90, 40)] * 4


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "output"
        self.output.mkdir()

    def clear_output(self):
        for path in self.output.iterdir():
            path.unlink()

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow must be restored")
    def test_when_tiff_has_multiple_frames_then_scrub_all_frames_and_remove_metadata(self):
        from PIL import Image

        source = self.root / "input.tiff"
        first = Image.new("RGB", (120, 60), "white")
        second = Image.new("RGB", (120, 60), "red")
        first.save(source, save_all=True, append_images=[second], description="sensitive-owner")
        for mode in ("annotate", "redact", "sanitize"):
            self.clear_output()
            manifest = process(source, self.output, mode, detect_value, fixed_ocr)
            with Image.open(self.output / "document.tiff") as result:
                self.assertEqual(result.n_frames, 2)
                for index in range(2):
                    result.seek(index)
                    self.assertNotIn(270, result.tag_v2)
                    if mode != "annotate":
                        self.assertEqual(result.convert("RGB").getpixel((80, 35)), (0, 0, 0))
            self.assertEqual(manifest["warnings"], [])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow must be restored")
    def test_when_image_redacted_then_original_pixels_and_metadata_are_not_retained(self):
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo

        for suffix, format_name in (("png", "PNG"), ("jpg", "JPEG")):
            for mode in ("annotate", "redact", "sanitize"):
                with self.subTest(suffix=suffix, mode=mode):
                    self.clear_output()
                    source = self.root / ("input." + suffix)
                    image = Image.new("RGB", (120, 60), "white")
                    metadata = PngInfo()
                    metadata.add_text("author", "sensitive-owner")
                    image.save(source, format=format_name, pnginfo=metadata)
                    process(source, self.output, mode, detect_value, fixed_ocr)
                    with Image.open(self.output / ("document." + suffix)) as result:
                        self.assertNotIn("author", result.info)
                        if mode != "annotate":
                            # Interior of a large covered box stays black after JPEG encoding.
                            self.assertEqual(result.convert("RGB").getpixel((75, 30)), (0, 0, 0))

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow must be restored")
    def test_when_label_cannot_fit_then_fail_without_publishing_a_successful_artifact(self):
        from PIL import Image

        unit = Unit("Mary", {}, [(0, 0, 1, 1)] * 4)
        detect([unit], detect_value)
        with self.assertRaisesRegex(JobError, "label_render_overflow"):
            paint(Image.new("RGB", (100, 100)), [unit], "sanitize", Labels([unit]))

    @unittest.skipUnless(
        importlib.util.find_spec("openpyxl") and importlib.util.find_spec("defusedxml"),
        "openpyxl and defusedxml must be restored",
    )
    def test_when_xlsx_has_hidden_numeric_pii_then_rebuild_all_cells_as_literal_values(self):
        from openpyxl import Workbook, load_workbook

        source = self.root / "input.xlsx"
        book = Workbook()
        book.active.title = "Mary"
        book.active["A1"] = "Mary"
        book.active["B2"] = 123456789
        book.active["C3"] = 42
        book.active["D4"] = "=Mary"
        book.active["D4"].data_type = "s"
        hidden = book.create_sheet("Private Mary")
        hidden.sheet_state = "hidden"
        hidden["Z100"] = "Mary"
        book.save(source)
        ocr = Mock(side_effect=AssertionError("Cells bypass OCR"))
        process(source, self.output, "sanitize", detect_value, ocr)
        rebuilt = load_workbook(self.output / "document.xlsx", data_only=False)
        self.assertEqual(rebuilt.sheetnames, ["Sheet 1", "Sheet 2"])
        self.assertEqual(rebuilt.worksheets[0]["A1"].value, "Person 1")
        self.assertEqual(rebuilt.worksheets[0]["B2"].value, "Entity US_SSN 1")
        self.assertEqual(rebuilt.worksheets[0]["C3"].value, 42)
        self.assertEqual(rebuilt.worksheets[0]["C3"].data_type, "n")
        self.assertEqual(rebuilt.worksheets[0]["D4"].value, "=Person 1")
        self.assertEqual(rebuilt.worksheets[0]["D4"].data_type, "s")
        self.assertEqual(rebuilt.worksheets[1]["Z100"].value, "Person 1")
        self.assertEqual(rebuilt.worksheets[1].sheet_state, "visible")
        with zipfile.ZipFile(self.output / "document.xlsx") as archive:
            self.assertNotIn(b"Mary", b"".join(archive.read(n) for n in archive.namelist()))
        ocr.assert_not_called()

    @unittest.skipUnless(
        importlib.util.find_spec("openpyxl") and importlib.util.find_spec("defusedxml"),
        "openpyxl and defusedxml must be restored",
    )
    def test_when_xlsx_has_formula_or_unparsed_part_then_reject_original_package(self):
        from openpyxl import Workbook

        source = self.root / "input.xlsx"
        book = Workbook()
        book.active["A1"] = "=1+1"
        book.save(source)
        with self.assertRaisesRegex(JobError, "unsupported_workbook_feature"):
            xlsx_preflight(source)
        book.active["A1"] = "Mary"
        book.save(source)
        with zipfile.ZipFile(source, "a") as archive:
            archive.writestr("xl/drawings/drawing1.xml", "<drawing/>")
        with self.assertRaisesRegex(JobError, "unsupported_workbook_part"):
            xlsx_preflight(source)

    @unittest.skipUnless(
        importlib.util.find_spec("openpyxl") and importlib.util.find_spec("defusedxml"),
        "openpyxl and defusedxml must be restored",
    )
    def test_when_workbook_protection_is_enabled_then_reject(self):
        from openpyxl import Workbook

        source = self.root / "protected.xlsx"
        book = Workbook()
        book.active["A1"] = "Mary"
        book.security.lockStructure = True
        book.save(source)
        with self.assertRaisesRegex(JobError, "unsupported_workbook_feature"):
            xlsx_preflight(source)

    @unittest.skipUnless(
        importlib.util.find_spec("openpyxl") and importlib.util.find_spec("defusedxml"),
        "openpyxl and defusedxml must be restored",
    )
    def test_when_xlsx_is_annotated_or_redacted_then_preserve_literal_types_and_coordinates(self):
        from openpyxl import Workbook, load_workbook

        source = self.root / "input.xlsx"
        book = Workbook()
        book.active["A1"] = "Mary"
        book.active["B2"] = 123456789
        book.active["C3"] = True
        book.active["D4"] = 42
        book.save(source)
        for mode in ("annotate", "redact"):
            with self.subTest(mode=mode):
                self.clear_output()
                process(source, self.output, mode, detect_value, fixed_ocr)
                rebuilt = load_workbook(self.output / "document.xlsx", data_only=False)
                self.addCleanup(rebuilt.close)
                sheet = rebuilt.active
                self.assertEqual(sheet["C3"].value, True)
                self.assertEqual(sheet["C3"].data_type, "b")
                self.assertEqual(sheet["D4"].value, 42)
                self.assertEqual(sheet["D4"].data_type, "n")
                if mode == "annotate":
                    self.assertEqual(sheet["A1"].value, "Mary")
                    self.assertEqual(sheet["B2"].value, 123456789)
                    self.assertEqual(sheet["A1"].fill.fgColor.rgb, "00FFFF00")
                else:
                    self.assertEqual(sheet["A1"].value, "[REDACTED]")
                    self.assertEqual(sheet["B2"].value, "[REDACTED]")
                    self.assertEqual(sheet["B2"].data_type, "s")

    @unittest.skipUnless(
        importlib.util.find_spec("PIL") and importlib.util.find_spec("pymupdf"),
        "Pillow and PyMuPDF must be restored",
    )
    def test_when_pdf_is_processed_then_all_pages_native_and_scanned_use_azure_provider_only(self):
        import pymupdf
        from PIL import Image

        source = self.root / "input.pdf"
        stream = io.BytesIO()
        Image.new("RGB", (120, 60), "white").save(stream, format="PNG")
        with pymupdf.open() as book:
            native = book.new_page(width=200, height=100)
            native.insert_text((20, 40), "Mary")
            scanned = book.new_page(width=200, height=100)
            scanned.insert_image(scanned.rect, stream=stream.getvalue())
            book.save(source)
        for mode in ("annotate", "redact", "sanitize"):
            with self.subTest(mode=mode):
                self.clear_output()
                ocr = Mock(side_effect=fixed_ocr)
                with patch(
                    "ase_document_tools._ocr", side_effect=AssertionError("Tesseract is forbidden")
                ):
                    process(source, self.output, mode, detect_value, ocr)
                self.assertEqual(ocr.call_count, 2)
                with pymupdf.open(self.output / "document.pdf") as result:
                    self.assertEqual(len(result), 2)
                    if mode != "annotate":
                        self.assertEqual("".join(page.get_text() for page in result), "")
                    else:
                        self.assertTrue(list(result[0].annots()))
                self.assertEqual(
                    json.loads((self.output / "manifest.json").read_text())["operation"], mode
                )


if __name__ == "__main__":
    unittest.main()
