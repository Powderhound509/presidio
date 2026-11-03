"""Synthetic document tests: real DOCX packages and optional raster/PDF integration."""

import importlib.util
import io
import json
import math
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET


SAMPLES = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ase_document_tools", SAMPLES / "ase_document_tools.py")
TOOLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TOOLS)
W, R = TOOLS.W, TOOLS.R
NS = {"w": W}


def detect(text):
    results = []
    offset = 0
    while "secret" in text[offset:]:
        start = text.index("secret", offset)
        results.append({"start": start, "end": start + 6, "score": 0.85, "entity_type": "TEST"})
        offset = start + 6
    return results


def paragraph(*texts):
    root = ET.Element(f"{{{W}}}p")
    for text in texts:
        run = ET.SubElement(root, f"{{{W}}}r")
        ET.SubElement(run, f"{{{W}}}t").text = text
    return root


def write_word(path, body_items, extra=None):
    document = ET.Element(f"{{{W}}}document")
    body = ET.SubElement(document, f"{{{W}}}body")
    for item in body_items:
        body.append(item)
    ET.SubElement(body, f"{{{W}}}sectPr")
    files = {
        "word/document.xml": ET.tostring(document, encoding="utf-8"),
        "[Content_Types].xml": (
            f'<Types xmlns="{TOOLS.CT}"><Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>'
        ).encode(),
        "_rels/.rels": (
            f'<Relationships xmlns="{TOOLS.PKG}"><Relationship Id="document" '
            f'Type="{R}/officeDocument" Target="word/document.xml"/></Relationships>'
        ).encode(),
    }
    files.update(extra or {})
    path.write_bytes(TOOLS._zip(files))


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.source = Path(self.folder.name) / "input.docx"
        self.target = Path(self.folder.name) / "output.docx"
        write_word(self.source, [paragraph("before se", "cret after")])

    def test_when_annotation_crosses_runs_then_only_matched_text_is_highlighted(self):
        original = self.source.read_bytes()
        summary = TOOLS.annotate_word(self.source, self.target, detect)
        self.assertEqual(summary, {"text_detections": 1, "image_detections": 0})
        with zipfile.ZipFile(self.target) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
        highlighted = [
            "".join(run.itertext())
            for run in root.iter(f"{{{W}}}r")
            if run.find("w:rPr/w:highlight", NS) is not None
        ]
        self.assertEqual(highlighted, ["se", "cret"])
        self.assertEqual("".join(root.itertext()), "before secret after")
        self.assertEqual(self.source.read_bytes(), original)

    def test_when_redaction_crosses_runs_then_original_text_is_removed_not_hidden(self):
        summary = TOOLS.redact_word(self.source, self.target, detect)
        self.assertEqual(summary, {"text_detections": 1, "image_detections": 0})
        with zipfile.ZipFile(self.target) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
            self.assertNotIn(b"secret", b"".join(archive.read(name) for name in archive.namelist()))
        self.assertEqual("".join(root.itertext()), "before [REDACTED] after")

    def test_when_annotation_preserves_compatibility_attributes_then_prefixes_remain_bound(self):
        with zipfile.ZipFile(self.source) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        files["word/document.xml"] = (
            f'<w:document xmlns:w="{W}" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" mc:Ignorable="w14">'
            '<w:body><w:p><w:r><w:t>secret</w:t></w:r></w:p><w:sectPr/></w:body></w:document>'
        ).encode()
        self.source.write_bytes(TOOLS._zip(files))
        TOOLS.annotate_word(self.source, self.target, detect)
        with zipfile.ZipFile(self.target) as archive:
            namespaces = [
                value for event, value in ET.iterparse(
                    io.BytesIO(archive.read("word/document.xml")), events=("start-ns",)
                )
            ]
        self.assertIn(("w14", "http://schemas.microsoft.com/office/word/2010/wordml"), namespaces)

    def test_when_one_run_contains_multiple_matches_then_annotations_preserve_all_text(self):
        write_word(self.source, [paragraph("a secret b secret c")])
        TOOLS.annotate_word(self.source, self.target, detect)
        with zipfile.ZipFile(self.target) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
        self.assertEqual("".join(root.itertext()), "a secret b secret c")
        highlighted = [
            "".join(run.itertext()) for run in root.iter(f"{{{W}}}r")
            if run.find("w:rPr/w:highlight", NS) is not None
        ]
        self.assertEqual(highlighted, ["secret", "secret"])

    def test_when_input_has_metadata_comments_hidden_runs_then_rebuilt_output_drops_them(self):
        body = paragraph("visible secret")
        hidden = ET.SubElement(body, f"{{{W}}}r")
        props = ET.SubElement(hidden, f"{{{W}}}rPr")
        ET.SubElement(props, f"{{{W}}}vanish")
        ET.SubElement(hidden, f"{{{W}}}t").text = "private-hidden"
        comment = ET.SubElement(body, f"{{{W}}}r")
        ET.SubElement(comment, f"{{{W}}}commentReference", {f"{{{W}}}id": "0"})
        write_word(self.source, [body], {
            "docProps/core.xml": b"<properties>private-owner</properties>",
            "word/comments.xml": b"<comments>private-comment</comments>",
            "customXml/item1.xml": b"<custom>private-custom</custom>",
            "word/headerUnused.xml": ET.tostring(paragraph("private-orphan")),
        })
        TOOLS.redact_word(self.source, self.target, detect)
        with zipfile.ZipFile(self.target) as archive:
            contents = b"".join(archive.read(name) for name in archive.namelist())
            self.assertNotIn("docProps/core.xml", archive.namelist())
            self.assertNotIn("word/comments.xml", archive.namelist())
            self.assertNotIn(b"private-", contents)

    def test_when_tables_headers_footers_exist_then_text_is_processed_in_each(self):
        table = ET.Element(f"{{{W}}}tbl")
        row = ET.SubElement(table, f"{{{W}}}tr")
        cell = ET.SubElement(row, f"{{{W}}}tc")
        cell.append(paragraph("table secret"))
        write_word(self.source, [table])
        with zipfile.ZipFile(self.source) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        document = ET.fromstring(files["word/document.xml"])
        section = document.find("w:body/w:sectPr", NS)
        rels = ET.Element(f"{{{TOOLS.PKG}}}Relationships")
        for kind in ("header", "footer"):
            root = ET.Element(f"{{{W}}}{'hdr' if kind == 'header' else 'ftr'}")
            root.append(paragraph(kind + " secret"))
            files[f"word/{kind}1.xml"] = ET.tostring(root)
            ET.SubElement(rels, f"{{{TOOLS.PKG}}}Relationship", {
                "Id": kind, "Type": R + "/" + kind, "Target": kind + "1.xml",
            })
            ET.SubElement(section, f"{{{W}}}{kind}Reference", {
                f"{{{R}}}id": kind, f"{{{W}}}type": "default",
            })
        files["word/document.xml"] = ET.tostring(document)
        files["word/_rels/document.xml.rels"] = ET.tostring(rels)
        self.source.write_bytes(TOOLS._zip(files))
        summary = TOOLS.redact_word(self.source, self.target, detect)
        self.assertEqual(summary["text_detections"], 3)
        with zipfile.ZipFile(self.target) as archive:
            for name in archive.namelist():
                self.assertNotIn(b"secret", archive.read(name))
            targets = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
            for rel in targets:
                self.assertIn("word/" + rel.get("Target"), archive.namelist())

    def test_when_input_has_unsupported_content_then_no_output_is_published(self):
        for tag in ("fldSimple", "ins", "txbxContent", "sdt", "object"):
            p = paragraph("secret")
            ET.SubElement(p, f"{{{W}}}{tag}")
            write_word(self.source, [p])
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                TOOLS.redact_word(self.source, self.target, detect)
            self.assertFalse(self.target.exists())

    def test_when_output_exists_then_it_is_not_replaced(self):
        self.target.write_bytes(b"existing output")
        with self.assertRaises(FileExistsError):
            TOOLS.redact_word(self.source, self.target, detect)
        self.assertEqual(self.target.read_bytes(), b"existing output")

    def test_when_detector_fails_then_no_partial_output_or_conversion_temp_remains(self):
        def fail(text):
            raise RuntimeError("synthetic service failure")
        with self.assertRaisesRegex(RuntimeError, "service failure"):
            TOOLS.redact_word(self.source, self.target, fail)
        self.assertFalse(self.target.exists())
        self.assertEqual(sorted(path.name for path in self.source.parent.iterdir()), ["input.docx"])

    def test_when_doc_conversion_fails_then_converter_output_is_not_exposed(self):
        source = self.source.with_suffix(".doc")
        source.write_bytes(b"synthetic legacy input")
        with patch.object(TOOLS.shutil, "which", return_value="synthetic-soffice"), patch.object(
            TOOLS.subprocess, "run", return_value=SimpleNamespace(returncode=1)
        ) as run:
            with self.assertRaisesRegex(RuntimeError, "conversion failed"):
                with TOOLS._word_input(source, "soffice"):
                    self.fail("Failed conversion must not yield an input")
            args = run.call_args.args[0]
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertIn("docx:Office Open XML Text", args)
            self.assertEqual(run.call_args.kwargs["timeout"], 120)

    def test_when_doc_conversion_succeeds_then_temp_docx_is_removed_after_use(self):
        source = self.source.with_suffix(".doc")
        source.write_bytes(b"synthetic legacy input")
        converted = None
        def convert(args, **kwargs):
            folder = Path(args[args.index("--outdir") + 1])
            write_word(folder / (source.stem + ".docx"), [paragraph("secret")])
            return SimpleNamespace(returncode=0)
        with patch.object(TOOLS.shutil, "which", return_value="synthetic-soffice"), patch.object(
            TOOLS.subprocess, "run", side_effect=convert
        ):
            with TOOLS._word_input(source, "soffice") as converted:
                self.assertTrue(converted.is_file())
            self.assertFalse(converted.exists())

    def test_when_offsets_or_scores_are_invalid_then_they_are_rejected(self):
        valid = {"start": 0, "end": 6, "score": 0.85, "entity_type": "TEST"}
        for change in ({"start": True}, {"end": 7}, {"score": math.nan}, {"score": math.inf}, {"score": 10 ** 400}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                TOOLS._detections([valid | change], "secret")
        self.assertEqual(TOOLS._intervals([
            valid, {"start": 2, "end": 8, "score": 0.85, "entity_type": "TEST"}
        ], "secretxx"), [(0, 8)])

    def test_when_ase_client_runs_then_token_transport_and_cleanup_are_correct(self):
        credential = Mock()
        credential.get_token.return_value = SimpleNamespace(token="synthetic-token")
        session = Mock()
        session.post.return_value = SimpleNamespace(status_code=200, json=lambda: detect("secret"))
        identity = Mock(return_value=credential)
        azure_error = type("SyntheticAzureError", (Exception,), {})
        request_error = type("SyntheticRequestError", (Exception,), {})
        modules = {
            "requests": SimpleNamespace(
                Session=Mock(return_value=session),
                exceptions=SimpleNamespace(RequestException=request_error),
            ),
            "azure": SimpleNamespace(),
            "azure.identity": SimpleNamespace(ManagedIdentityCredential=identity),
            "azure.core": SimpleNamespace(),
            "azure.core.exceptions": SimpleNamespace(AzureError=azure_error),
        }
        with patch.dict(sys.modules, modules):
            with TOOLS.ASEAnalyzer("https://analyzer.example.test", "api://test-api", "umi-client") as client:
                self.assertEqual(client.analyze("secret"), detect("secret"))
                session.post.side_effect = request_error("private-body")
                with self.assertRaisesRegex(RuntimeError, "transport failed") as error:
                    client.analyze("secret")
                self.assertNotIn("private-body", str(error.exception))
                self.assertTrue(error.exception.__suppress_context__)
        identity.assert_called_once_with(client_id="umi-client")
        credential.get_token.assert_called_with("api://test-api/.default")
        self.assertEqual(session.post.call_args.kwargs["json"], {"text": "secret", "language": "en"})
        self.assertEqual(session.post.call_args.kwargs["headers"], {"Authorization": "Bearer synthetic-token"})
        self.assertTrue(session.post.call_args.kwargs["verify"])
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
        self.assertEqual(session.post.call_args.kwargs["timeout"], (10, 60))
        session.close.assert_called_once()
        credential.close.assert_called_once()

    def test_when_xml_has_utf16_entity_declarations_then_it_is_rejected(self):
        payload = '<!DOCTYPE root [<!ENTITY value "private">]><root>&value;</root>'
        with self.assertRaisesRegex(ValueError, "DTD/entity"):
            TOOLS._xml(payload.encode("utf-16"))

    def test_when_ocr_tsv_has_words_then_character_geometry_keeps_word_boundaries(self):
        image = SimpleNamespace(
            width=100, height=100, save=lambda stream, **kwargs: stream.write(b"synthetic PNG"),
            getextrema=lambda: ((0, 255), (0, 255), (0, 255)),
        )
        header = "block_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        data = (header + "1\t1\t1\t10\t20\t30\t10\t90\tsecret\n"
                + "1\t1\t2\t10\t40\t20\t10\t95\tword\n").encode()
        with patch.object(TOOLS.shutil, "which", return_value="synthetic-tesseract"), patch.object(
            TOOLS.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=data)
        ):
            text, geometry = TOOLS._ocr(image, language="eng", command="tesseract", min_confidence=50)
        self.assertEqual(text, "secret\nword")
        self.assertEqual(geometry, [(10, 20, 40, 30)] * 6 + [None] + [(10, 40, 30, 50)] * 4)

    def test_when_ocr_confidence_is_low_or_no_text_found_then_output_is_refused(self):
        image = SimpleNamespace(
            width=100, height=100, save=lambda stream, **kwargs: stream.write(b"PNG"),
            getextrema=lambda: ((0, 255), (0, 255), (0, 255)),
        )
        header = "block_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        for output in (header, header + "1\t1\t1\t10\t20\t30\t10\t10\tsecret\n"):
            with self.subTest(output_length=len(output)), patch.object(
                TOOLS.shutil, "which", return_value="synthetic-tesseract"
            ), patch.object(TOOLS.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=output.encode()
            )), self.assertRaises(RuntimeError):
                TOOLS._ocr(image, language="eng", command="tesseract", min_confidence=50)

    def test_when_new_notebooks_are_loaded_then_cells_compile_and_outputs_are_clear(self):
        for name in ("presidio_word_annotation", "presidio_word_redaction", "presidio_pdf_redaction"):
            notebook = json.loads((SAMPLES / (name + ".ipynb")).read_text(encoding="utf-8"))
            self.assertEqual(notebook["nbformat"], 4)
            for cell in notebook["cells"]:
                if cell["cell_type"] == "code":
                    compile("".join(cell["source"]), cell["id"], "exec")
                    self.assertIsNone(cell["execution_count"])
                    self.assertEqual(cell["outputs"], [])


@unittest.skipUnless(
    importlib.util.find_spec("PIL") and importlib.util.find_spec("pymupdf"),
    "Install Pillow and PyMuPDF to run real raster/PDF integration tests.",
)
class RasterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.source = Path(self.folder.name) / "input.docx"
        self.target = Path(self.folder.name) / "output.docx"

    def test_when_pdf_has_text_metadata_and_attachment_then_output_contains_only_scrubbed_pages(self):
        import pymupdf
        from PIL import Image
        source = self.source.with_suffix(".pdf")
        target = self.target.with_suffix(".pdf")
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=200, height=200)
            page.insert_text((40, 80), "secret", fontsize=20)
            pdf.set_metadata({"author": "private-owner"})
            pdf.embfile_add("private-attachment", b"private-body")
            pdf.save(source)
        with patch.object(TOOLS, "_ocr", return_value=("", [])):
            TOOLS.redact_pdf(source, target, detect, dpi=144)
        with pymupdf.open(target) as pdf:
            self.assertEqual(pdf[0].get_text(), "")
            self.assertEqual(pdf.embfile_count(), 0)
            self.assertEqual(pdf.metadata["author"], "")
            pixmap = pdf[0].get_pixmap(dpi=144)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            self.assertEqual(image.getpixel((85, 150)), (0, 0, 0))

    def test_when_image_contains_detected_word_then_pixels_are_removed_and_not_just_outlined(self):
        from PIL import Image
        image = Image.new("RGB", (100, 100), "white")
        count = TOOLS._paint(image, "secret", [(10, 20, 40, 30)] * 6, detect, redact=True)
        self.assertEqual(count, 1)
        for point in ((10, 20), (25, 25), (40, 30)):
            self.assertEqual(image.getpixel(point), (0, 0, 0))
        self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))

    def test_when_word_picture_is_redacted_then_original_image_metadata_and_alt_text_are_absent(self):
        from PIL import Image, PngImagePlugin
        image = Image.new("RGB", (100, 100), "white")
        image_data = io.BytesIO()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("author", "private-image-owner")
        image.save(image_data, format="PNG", pnginfo=metadata)
        p = paragraph("secret")
        run = TOOLS._picture_xml("picture", "914400", "914400", 1)
        run.find(".//wp:docPr", TOOLS.NS).set("descr", "private-alt")
        p.append(run)
        rels = (
            f'<Relationships xmlns="{TOOLS.PKG}"><Relationship Id="picture" '
            f'Type="{R}/image" Target="media/image.png"/></Relationships>'
        ).encode()
        write_word(self.source, [p], {
            "word/_rels/document.xml.rels": rels,
            "word/media/image.png": image_data.getvalue(),
        })
        with patch.object(TOOLS, "_ocr", return_value=("secret", [(10, 20, 40, 30)] * 6)):
            TOOLS.redact_word(self.source, self.target, detect)
        with zipfile.ZipFile(self.target) as archive:
            all_bytes = b"".join(archive.read(name) for name in archive.namelist())
            self.assertNotIn(b"private-alt", all_bytes)
            self.assertNotIn(b"private-image-owner", all_bytes)
            with Image.open(io.BytesIO(archive.read("word/media/image1.png"))) as result:
                self.assertEqual(result.getpixel((25, 25)), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
