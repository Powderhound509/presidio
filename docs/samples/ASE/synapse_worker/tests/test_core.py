"""Executable standard-library tests; no Azure access or real PII is needed."""

import codecs
import json
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace as dataclass_replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT.parent))

from presidio_document_worker.client import Client, configure_storage
from presidio_document_worker.config import Config
from presidio_document_worker.contract import JobError, Submission, parse_message, queue_message
from presidio_document_worker.entities import Labels, Unit, annotations, detect, replace
from presidio_document_worker.formats import process
from presidio_document_worker.ocr import normalize
from presidio_document_worker.runtime import Worker

ID = "00000000-0000-0000-0000-000000000003"
VERSION = "00000000-0000-0000-0000-000000000004"
ETAG = '"0x000000000000001"'


def detector(text):
    results = []
    for value, entity in (
        ("Mary Smith", "PERSON"),
        ("Mary", "PERSON"),
        ("Jones", "PERSON"),
        ("alice@example.com", "EMAIL_ADDRESS"),
        ("123456789", "US_SSN"),
    ):
        offset = 0
        while value in text[offset:]:
            start = text.index(value, offset)
            results.append(
                {"start": start, "end": start + len(value), "entity_type": entity, "score": 0.9}
            )
            offset = start + len(value)
    return results


class ContractTests(unittest.TestCase):
    def test_when_submission_is_valid_then_only_opaque_input_reference_is_derived(self):
        item = Submission(ID, VERSION, "pdf", ETAG, "redact")
        self.assertEqual(item.blob, f"{ID}/{VERSION}.pdf")
        self.assertEqual(parse_message(queue_message(ID)), ID)
        self.assertEqual(Submission(ID, VERSION, "pdf", "0x1", "redact").etag, '"0x1"')
        with self.assertRaises(JobError):
            Submission(ID, VERSION, "pdf", '"0x1', "redact")

    def test_when_untrusted_parameters_are_invalid_then_fail_without_echoing_them(self):
        for field, value in (
            ("document_id", "../sensitive"),
            ("extension", "xlsm"),
            ("operation", "exec"),
            ("language", "eng"),
            ("etag", "https://private?sas=sensitive"),
        ):
            values = dict(
                document_id=ID, version_id=VERSION, extension="pdf", etag=ETAG, operation="redact"
            )
            values[field] = value
            with self.subTest(field=field), self.assertRaises(JobError) as error:
                Submission(**values)
            self.assertNotIn("sensitive", str(error.exception))
        for message in ("2:" + ID, ID, "1:../file", "1:" + ID + "/"):
            with self.assertRaises(JobError):
                parse_message(message)

    def test_when_credentials_or_identity_fallbacks_are_configured_then_reject(self):
        values = json.loads((ROOT / "config.example.json").read_text())
        Config(**values)
        for change in (
            {"identity": "default"},
            {"client_id": ""},
            {"identity": "system"},
            {"processing_enabled": True},
            {"blob_endpoint": "https://host/path?sig=sensitive"},
            {"sql_server": "host;Password=sensitive"},
            {"ocr_min_confidence": float("nan")},
            {"ocr_scope": "https://cognitiveservices.azure.com/.default"},
        ):
            with self.subTest(change=change), self.assertRaises(JobError):
                Config(**(values | change))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(values | {"password": "sensitive"}))
            with self.assertRaises(JobError) as error:
                Config.load(path)
            self.assertEqual(str(error.exception), "invalid_configuration")


class EntityTests(unittest.TestCase):
    def units(self, *texts):
        units = [Unit(text, {"text": index}) for index, text in enumerate(texts)]
        detect(units, detector)
        return units

    def test_when_explicit_alias_is_detected_then_reuse_labels_and_reset_next_file(self):
        units = self.units("Mary Smith (Mary)", "Mary and Jones")
        labels = Labels(units)
        self.assertEqual(replace(units[0], "sanitize", labels), "Person 1 (Person 1)")
        self.assertEqual(replace(units[1], "sanitize", labels), "Person 1 and Person 2")
        self.assertEqual(labels.warnings, [])
        fresh = self.units("Jones")
        self.assertEqual(replace(fresh[0], "sanitize", Labels(fresh)), "Person 1")

    def test_when_name_similarity_has_no_explicit_evidence_then_keep_separate_and_warn(self):
        units = self.units("Mary Smith and Mary")
        labels = Labels(units)
        self.assertEqual(replace(units[0], "sanitize", labels), "Person 1 and Person 2")
        self.assertEqual(labels.warnings, ["uncertain_aliases"])

    def test_when_initial_and_surname_are_similar_then_flag_without_inferred_merge(self):
        text = "Mary Smith met M. Smith"
        unit = Unit(text, {})
        detect(
            [unit],
            lambda _: [
                {"start": 0, "end": 10, "entity_type": "PERSON", "score": 0.9},
                {"start": 15, "end": 23, "entity_type": "PERSON", "score": 0.9},
            ],
        )
        labels = Labels([unit])
        self.assertEqual(labels.warnings, ["uncertain_aliases"])
        self.assertEqual(replace(unit, "sanitize", labels), "Person 1 met Person 2")

    def test_when_alias_declarations_conflict_then_do_not_merge(self):
        def custom(text):
            return [
                {"start": s, "end": e, "entity_type": "PERSON", "score": 1.0}
                for s, e in ((0, 10), (12, 16), (19, 29), (31, 35))
            ]

        units = [Unit("Mary Smith (Mary), Mary Jones (Mary)", {})]
        detect(units, custom)
        labels = Labels(units)
        self.assertEqual(labels.warnings, ["uncertain_aliases"])
        self.assertEqual(
            replace(units[0], "sanitize", labels), "Person 1 (Person 2), Person 3 (Person 2)"
        )

    def test_when_detections_overlap_then_cover_complete_union(self):
        unit = Unit("abcdef", {})
        detect(
            [unit],
            lambda _: [
                {"start": 1, "end": 4, "entity_type": "PERSON", "score": 0.8},
                {"start": 3, "end": 5, "entity_type": "LOCATION", "score": 0.9},
            ],
        )
        self.assertEqual(
            unit.detections, [{"start": 1, "end": 5, "entity_type": "PII", "score": 0.9}]
        )
        self.assertEqual(replace(unit, "redact", Labels([unit])), "a[REDACTED]f")

    def test_when_sidecar_is_written_then_only_offsets_types_scores_and_locations_remain(self):
        units = self.units("Contact alice@example.com.")
        self.assertEqual(
            annotations(units),
            [
                {
                    "location": {"text": 0},
                    "start": 8,
                    "end": 25,
                    "entity_type": "EMAIL_ADDRESS",
                    "score": 0.9,
                }
            ],
        )
        self.assertNotIn("alice", json.dumps(annotations(units)))

    def test_when_block_exceeds_limit_then_refuse_instead_of_missing_boundary_entities(self):
        analyze = Mock()
        with self.assertRaises(JobError) as error:
            detect([Unit("x" * 100001, {})], analyze)
        self.assertEqual(str(error.exception), "analysis_block_limit")
        analyze.assert_not_called()


class OcrTests(unittest.TestCase):
    def result(self):
        return {
            "content": "\U0001f600 Mary",
            "pages": [
                {
                    "pageNumber": 1,
                    "width": 100,
                    "height": 50,
                    "unit": "pixel",
                    "words": [
                        {
                            "content": "\U0001f600",
                            "confidence": 0.9,
                            "span": {"offset": 0, "length": 1},
                            "polygon": [0, 0, 10, 0, 10, 20, 0, 20],
                        },
                        {
                            "content": "Mary",
                            "confidence": 0.95,
                            "span": {"offset": 2, "length": 4},
                            "polygon": [20, 0, 60, 0, 60, 20, 20, 20],
                        },
                    ],
                }
            ],
        }

    def test_when_unicode_codepoint_spans_are_used_then_python_geometry_aligns(self):
        text, geometry = normalize(self.result(), (100, 50), 0.8)
        self.assertEqual(text, "\U0001f600 Mary")
        self.assertEqual(geometry, [(0, 0, 10, 20), None] + [(20, 0, 60, 20)] * 4)

    def test_when_threshold_boundary_is_met_then_accept_exactly(self):
        self.assertEqual(normalize(self.result(), (100, 50), 0.9)[0], "\U0001f600 Mary")
        with self.assertRaisesRegex(JobError, "low_ocr_confidence"):
            normalize(self.result(), (100, 50), 0.900001)

    def test_when_ocr_coverage_units_offsets_or_geometry_are_invalid_then_refuse(self):
        for mutate in (
            lambda r: r["pages"].append(dict(r["pages"][0])),
            lambda r: r["pages"][0].update(unit="inch"),
            lambda r: r["pages"][0].update(width=101),
            lambda r: r["pages"][0]["words"].pop(),
            lambda r: r["pages"][0]["words"][1]["span"].update(offset=3),
            lambda r: r["pages"][0]["words"][0].update(polygon=[-1] * 8),
            lambda r: r["pages"][0]["words"][0].update(confidence=float("nan")),
        ):
            result = self.result()
            mutate(result)
            with self.subTest(mutate=mutate), self.assertRaises(JobError):
                normalize(result, (100, 50), 0.8)


class NativeTextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "output"
        self.output.mkdir()

    def test_when_supported_text_encoding_is_used_then_preserve_bom_and_line_endings(self):
        for encoding, bom in (
            ("utf-8", b""),
            ("utf-8", codecs.BOM_UTF8),
            ("utf-16-le", codecs.BOM_UTF16_LE),
            ("utf-16-be", codecs.BOM_UTF16_BE),
        ):
            with self.subTest(encoding=encoding, bom=bom):
                for path in self.output.iterdir():
                    path.unlink()
                source = self.root / "input.txt"
                source.write_bytes(bom + "Mary\r\nMary\n".encode(encoding))
                ocr = Mock(side_effect=AssertionError("TXT must not use OCR"))
                manifest = process(source, self.output, "sanitize", detector, ocr)
                self.assertEqual(
                    (self.output / "document.txt").read_bytes(),
                    bom + "Person 1\r\nPerson 1\n".encode(encoding),
                )
                self.assertEqual(
                    manifest["artifacts"], [{"name": "document.txt", "content_type": "text/plain"}]
                )
                ocr.assert_not_called()

    def test_when_annotating_text_then_keep_original_and_emit_valueless_sidecar(self):
        source = self.root / "input.txt"
        source.write_bytes(b"Contact alice@example.com.")
        manifest = process(source, self.output, "annotate", detector, Mock())
        self.assertTrue(manifest["sensitive"])
        self.assertEqual((self.output / "document.txt").read_bytes(), source.read_bytes())
        sidecar = json.loads((self.output / "annotations.json").read_text())
        self.assertEqual(sidecar["annotations"][0]["start"], 8)
        self.assertEqual(sidecar["annotations"][0]["end"], 25)
        self.assertNotIn("alice", json.dumps(sidecar))

    def test_when_input_is_unsupported_encoding_or_disguised_pdf_then_refuse(self):
        source = self.root / "input.txt"
        for content in (b"\xff\xfe\x00", b"\x80", b"%PDF-1.7\n", b"a\x00b"):
            source.write_bytes(content)
            with self.subTest(content=content), self.assertRaises(JobError):
                process(source, self.output, "redact", detector, Mock())
            self.assertEqual(list(self.output.iterdir()), [])

    def test_when_docx_text_crosses_runs_then_reuse_word_rebuild_with_consistent_labels(self):
        import ase_document_tools as tools

        source = self.root / "input.docx"
        document = ET.Element(f"{{{tools.W}}}document")
        body = ET.SubElement(document, f"{{{tools.W}}}body")
        for texts in (("Ma", "ry"), ("Mary and Jones",), ("MaryJones",)):
            paragraph = ET.SubElement(body, f"{{{tools.W}}}p")
            for value in texts:
                run = ET.SubElement(paragraph, f"{{{tools.W}}}r")
                ET.SubElement(run, f"{{{tools.W}}}t").text = value
        ET.SubElement(body, f"{{{tools.W}}}sectPr")
        types = ET.Element(f"{{{tools.CT}}}Types")
        ET.SubElement(
            types,
            f"{{{tools.CT}}}Override",
            {
                "PartName": "/word/document.xml",
                "ContentType": (
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document.main+xml"
                ),
            },
        )
        source.write_bytes(
            tools._zip(
                {
                    "word/document.xml": ET.tostring(document),
                    "[Content_Types].xml": ET.tostring(types),
                    "_rels/.rels": f'<Relationships xmlns="{tools.PKG}"/>'.encode(),
                }
            )
        )
        ocr = Mock(side_effect=AssertionError("No image OCR expected"))
        with patch.object(tools, "_ocr", side_effect=AssertionError("No Tesseract allowed")):
            process(source, self.output, "sanitize", detector, ocr)
        with zipfile.ZipFile(self.output / "document.docx") as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
            self.assertEqual(
                ["".join(p.itertext()) for p in root.iter(f"{{{tools.W}}}p")],
                ["Person 1", "Person 1 and Person 2", "Person 1Person 2"],
            )
            self.assertNotIn(b"Mary", b"".join(archive.read(n) for n in archive.namelist()))
        ocr.assert_not_called()

    def test_when_word_image_provider_is_explicit_then_never_launch_tesseract(self):
        import ase_document_tools as tools

        image = Mock()
        provider = Mock(return_value=("Mary", [(1, 1, 10, 10)] * 4))
        files = {"word/media/image.png": b"synthetic"}
        with (
            patch.object(tools, "_image", return_value=(image, "PNG")),
            patch.object(tools, "_ocr", side_effect=AssertionError("Tesseract must never be used")),
            patch.object(tools, "_paint", return_value=1) as painting,
        ):
            count = tools._word_images(
                files,
                detector,
                redact=True,
                ocr_language="unused",
                tesseract="absent",
                min_confidence=50,
                ocr_provider=provider,
            )
        self.assertEqual(count, 1)
        provider.assert_called_once_with(image)
        painting.assert_called_once_with(image, "Mary", [(1, 1, 10, 10)] * 4, detector, redact=True)


class ClientAndQueueTests(unittest.TestCase):
    def test_when_storage_is_configured_then_use_explicit_sami_linked_service_oauth(self):
        spark = Mock()
        host = "approvedstorage.dfs.core.usgovcloudapi.net"
        configure_storage(spark, host, "ApprovedStorage")
        spark.conf.set.assert_called_once_with(
            f"spark.storage.synapse.{host}.linkedServiceName", "ApprovedStorage"
        )
        configuration = spark.sparkContext._jsc.hadoopConfiguration.return_value
        self.assertEqual(
            configuration.set.call_args_list[0].args,
            (f"fs.azure.account.auth.type.{host}", "OAuth"),
        )
        self.assertEqual(
            configuration.set.call_args_list[1].args,
            (
                f"fs.azure.account.oauth.provider.type.{host}",
                "com.microsoft.azure.synapse.tokenlibrary.LinkedServiceBasedTokenProvider",
            ),
        )

    def test_when_job_is_failed_then_batch_must_not_report_success(self):
        execute = Mock(return_value=[{"job_id": ID, "status": "dead_letter"}])
        client = Client(execute)
        with self.assertRaisesRegex(JobError, "document_processing_failed"):
            client.wait(ID, timeout=1, interval=1)
        execute.assert_called_once_with("Status", ID)

    def test_when_status_not_owned_then_do_not_return_any_output_reference(self):
        with self.assertRaisesRegex(JobError, "job_not_authorized_or_missing"):
            Client(Mock(return_value=[])).status(ID)

    def worker(self, responses):
        config = SimpleNamespace(lease_seconds=180, max_attempts=3)
        sql, queue, stop = Mock(), Mock(), Mock()
        sql.call.side_effect = responses
        return Worker(config, sql, queue, stop), sql, queue

    def test_when_dispatching_then_publish_only_versioned_id_with_nonexpiring_ttl(self):
        worker, sql, queue = self.worker([[], [{"job_id": ID, "dispatch_token": VERSION}], []])
        worker.dispatch()
        queue.send_message.assert_called_once_with("1:" + ID, time_to_live=-1)
        self.assertEqual(sql.call.call_args_list[-1].args, ("Dispatched", ID, VERSION))

    def test_when_duplicate_terminal_message_arrives_then_ack_without_processing(self):
        worker, _, queue = self.worker([[{"status": "succeeded"}]])
        message = SimpleNamespace(content="1:" + ID, id="message", pop_receipt="receipt")
        with patch.object(worker, "run_claim") as run:
            worker.consume(message)
        run.assert_not_called()
        queue.delete_message.assert_called_once_with("message", "receipt")

    def test_when_stale_worker_loses_fence_then_never_ack_or_commit(self):
        worker, sql, queue = self.worker(
            [
                [{"status": "running", "claimed": 1, "job_id": ID, "claim_token": VERSION}],
                [{"owned": 0}],
            ]
        )
        message = SimpleNamespace(content="1:" + ID, id="message", pop_receipt="old")
        outcome = ({"ok": True, "warnings": False}, "new")
        with patch.object(worker, "run_claim", return_value=outcome):
            worker.consume(message)
        queue.delete_message.assert_not_called()
        self.assertEqual(sql.call.call_args_list[-1].args, ("Complete", ID, VERSION, False))

    def test_when_artifact_publication_fails_then_retry_without_acknowledgement(self):
        worker, sql, queue = self.worker(
            [
                [{"status": "running", "claimed": 1, "job_id": ID, "claim_token": VERSION}],
                [{"status": "retry_wait"}],
            ]
        )
        message = SimpleNamespace(content="1:" + ID, id="message", pop_receipt="old")
        with patch.object(
            worker,
            "run_claim",
            return_value=({"ok": False, "code": "storage_failed", "retryable": True}, "new"),
        ):
            worker.consume(message)
        queue.delete_message.assert_not_called()
        self.assertEqual(
            sql.call.call_args_list[-1].args, ("Fail", ID, VERSION, "storage_failed", True, 3)
        )

    def test_when_child_exits_without_result_then_return_explicit_retry_and_close_handles(self):
        context, parent, child, process = Mock(), Mock(), Mock(), Mock()
        context.Pipe.return_value = (parent, child)
        context.Process.return_value = process
        process.is_alive.return_value = False
        parent.poll.return_value = True
        parent.recv.side_effect = EOFError
        stop = Mock()
        stop.is_set.return_value = False
        sql, queue = Mock(), Mock()
        worker = Worker(Config.load(ROOT / "config.example.json"), sql, queue, stop)
        message = SimpleNamespace(pop_receipt="receipt")
        with (
            patch(
                "presidio_document_worker.runtime.multiprocessing.get_context", return_value=context
            ),
            patch("presidio_document_worker.runtime.os.killpg", create=True),
        ):
            result = worker.run_claim({"job_id": ID, "claim_token": VERSION}, message)
        self.assertEqual(
            result,
            ({"ok": False, "code": "processing_process_exited", "retryable": True}, "receipt"),
        )
        parent.close.assert_called_once_with()
        child.close.assert_called_once_with()
        process.close.assert_called_once_with()
        sql.call.assert_not_called()
        queue.delete_message.assert_not_called()

    def test_when_sql_lease_is_lost_then_stop_child_without_acknowledgement(self):
        context, parent, child, process = Mock(), Mock(), Mock(), Mock()
        context.Pipe.return_value = (parent, child)
        context.Process.return_value = process
        process.is_alive.side_effect = [True, True, False]
        stop, sql, queue = Mock(), Mock(), Mock()
        stop.is_set.return_value = False
        sql.call.return_value = [{"owned": 0}]
        worker = Worker(Config.load(ROOT / "config.example.json"), sql, queue, stop)
        with (
            patch(
                "presidio_document_worker.runtime.multiprocessing.get_context", return_value=context
            ),
            patch("presidio_document_worker.runtime.os.killpg", create=True),
            patch("presidio_document_worker.runtime.time.monotonic", side_effect=[0, 61, 61, 61]),
            self.assertLogs("document_worker", level="ERROR") as logged,
        ):
            result = worker.run_claim(
                {"job_id": ID, "claim_token": VERSION},
                SimpleNamespace(pop_receipt="receipt"),
            )
        self.assertEqual(result, (None, "receipt"))
        self.assertEqual(logged.output, ["ERROR:document_worker:sql_claim_lost"])
        sql.call.assert_called_once_with("Renew", ID, VERSION, 180)
        queue.update_message.assert_not_called()
        queue.delete_message.assert_not_called()
        process.terminate.assert_called_once_with()
        process.join.assert_called_once_with(timeout=10)
        parent.close.assert_called_once_with()

    def test_when_job_wall_deadline_expires_then_stop_child_and_return_retryable_failure(self):
        context, parent, child, process = Mock(), Mock(), Mock(), Mock()
        context.Pipe.return_value = (parent, child)
        context.Process.return_value = process
        process.is_alive.side_effect = [True, True, False]
        stop, sql, queue = Mock(), Mock(), Mock()
        stop.is_set.return_value = False
        config = dataclass_replace(
            Config.load(ROOT / "config.example.json"), job_timeout_seconds=60
        )
        worker = Worker(config, sql, queue, stop)
        with (
            patch(
                "presidio_document_worker.runtime.multiprocessing.get_context", return_value=context
            ),
            patch("presidio_document_worker.runtime.os.killpg", create=True),
            patch("presidio_document_worker.runtime.time.monotonic", side_effect=[0, 61, 61]),
        ):
            result = worker.run_claim(
                {"job_id": ID, "claim_token": VERSION},
                SimpleNamespace(pop_receipt="receipt"),
            )
        self.assertEqual(
            result, ({"ok": False, "code": "job_deadline", "retryable": True}, "receipt")
        )
        process.terminate.assert_called_once_with()
        process.join.assert_called_once_with(timeout=10)
        sql.call.assert_not_called()
        queue.delete_message.assert_not_called()
        parent.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
