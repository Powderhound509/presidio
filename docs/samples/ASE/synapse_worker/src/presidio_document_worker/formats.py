"""PDF-first extraction and fresh native artifact writers with explicit limits."""

import codecs
import hashlib
import io
import json
import math
import re
import zipfile
from pathlib import Path, PurePosixPath

from .contract import (
    MAX_BYTES,
    MAX_CELLS,
    MAX_EXPANDED_BYTES,
    MAX_FRAMES,
    MAX_PIXELS,
    MAX_SHEETS,
    MAX_TOTAL_PIXELS,
    MAX_ZIP_ENTRIES,
    OPERATIONS,
    POLICY,
    JobError,
)
from .entities import Labels, Unit, annotations, detect, replace

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": XLSX_MIME,
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


def package(path):
    """Preflight bounded ZIP entries without extracting user-controlled filenames."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if (
                len(infos) > MAX_ZIP_ENTRIES
                or sum(item.file_size for item in infos) > MAX_EXPANDED_BYTES
                or len({item.filename for item in infos}) != len(infos)
            ):
                raise JobError("package_expansion_limit")
            for item in infos:
                parts = PurePosixPath(item.filename).parts
                if (
                    "\\" in item.filename
                    or ":" in item.filename
                    or item.filename.startswith("/")
                    or ".." in parts
                    or item.flag_bits & 1
                ):
                    raise JobError("unsafe_package")
            return {item.filename: archive.read(item) for item in infos}
    except (zipfile.BadZipFile, RuntimeError, EOFError):
        raise JobError("invalid_package") from None


def xlsx_preflight(path):
    """Reject unsupported OOXML before a workbook library can silently drop it."""
    from defusedxml import ElementTree as SafeET
    from defusedxml.common import DefusedXmlException

    files = package(path)
    if "xl/workbook.xml" not in files:
        raise JobError("format_mismatch")
    allowed = re.compile(
        r"(?:\[Content_Types\]\.xml|_rels/\.rels|docProps/(?:app|core)\.xml|"
        r"xl/(?:workbook\.xml|styles\.xml|sharedStrings\.xml|"
        r"theme/theme[0-9]+\.xml|_rels/workbook\.xml\.rels|worksheets/sheet[0-9]+\.xml))"
    )
    forbidden = {
        "f",
        "definedName",
        "sheetProtection",
        "extLst",
        "hyperlink",
        "dataValidation",
        "conditionalFormatting",
        "drawing",
        "legacyDrawing",
        "oleObject",
        "tablePart",
        "pivotTable",
        "r",
        "phoneticPr",
        "mergeCell",
        "protectedRange",
        "customSheetView",
    }
    workbook_tags = {
        "workbook",
        "workbookPr",
        "workbookProtection",
        "bookViews",
        "workbookView",
        "sheets",
        "sheet",
        "definedNames",
        "calcPr",
    }
    sheet_tags = {
        "worksheet",
        "sheetPr",
        "outlinePr",
        "pageSetUpPr",
        "dimension",
        "sheetViews",
        "sheetView",
        "selection",
        "sheetFormatPr",
        "cols",
        "col",
        "sheetData",
        "row",
        "c",
        "v",
        "is",
        "t",
        "pageMargins",
        "printOptions",
        "pageSetup",
    }
    count = 0
    for name, payload in files.items():
        if not allowed.fullmatch(name):
            raise JobError("unsupported_workbook_part")
        try:
            root = SafeET.fromstring(payload)
        except (SafeET.ParseError, ValueError, DefusedXmlException):
            raise JobError("invalid_package_xml") from None
        for node in root.iter():
            local = node.tag.rsplit("}", 1)[-1]
            # openpyxl emits an empty element even when protection is disabled.
            protected = local == "workbookProtection" and (
                node.attrib or len(node) or (node.text or "").strip()
            )
            if local in forbidden or protected or node.get("TargetMode") == "External":
                raise JobError("unsupported_workbook_feature")
            if (
                (name == "xl/workbook.xml" and local not in workbook_tags)
                or (name.startswith("xl/worksheets/") and local not in sheet_tags)
                or (name == "xl/sharedStrings.xml" and local not in {"sst", "si", "t"})
            ):
                raise JobError("unsupported_workbook_feature")
            if (
                name in ("xl/workbook.xml", "xl/sharedStrings.xml")
                or name.startswith("xl/worksheets/")
            ) and not node.tag.startswith("{" + S + "}"):
                raise JobError("unsupported_workbook_namespace")
            if local == "c":
                count += 1
                if count > MAX_CELLS:
                    raise JobError("workbook_cell_limit")
                if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", node.get("r", "")):
                    raise JobError("invalid_cell_coordinate")
    return files


def _image_rgb(original):
    from PIL import Image, ImageOps

    if original.width * original.height > MAX_PIXELS:
        raise JobError("image_pixel_limit")
    rgba = ImageOps.exif_transpose(original).convert("RGBA")
    background = Image.new("RGBA", rgba.size, "white")
    return Image.alpha_composite(background, rgba).convert("RGB")


def _boxes(unit, detection, size):
    if unit.geometry is None or len(unit.geometry) != len(unit.text):
        raise JobError("invalid_geometry")
    found = {box for box in unit.geometry[detection["start"] : detection["end"]] if box}
    if not found:
        raise JobError("unlocated_detection")
    width, height = size
    result = []
    for x0, y0, x1, y1 in found:
        if (
            not all(math.isfinite(v) for v in (x0, y0, x1, y1))
            or not 0 <= x0 < x1 <= width
            or not 0 <= y0 < y1 <= height
        ):
            raise JobError("invalid_geometry")
        result.append(
            (
                max(0, math.floor(x0) - 2),
                max(0, math.floor(y0) - 2),
                min(width - 1, math.ceil(x1) + 2),
                min(height - 1, math.ceil(y1) + 2),
            )
        )
    return sorted(result, key=lambda b: (b[1], b[0]))


def paint(image, units, mode, labels):
    """Erase all original pixels before rendering labels, failing closed on overflow."""
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(image)
    regions = []
    for unit in units:
        for d in unit.detections:
            boxes = _boxes(unit, d, image.size)
            for box in boxes:
                if mode == "annotate":
                    draw.rectangle(box, outline="yellow", width=3)
                else:
                    draw.rectangle(box, fill="black")
            if mode == "sanitize":
                regions.append((boxes[0], labels.replacement(unit, d)))
    used = []
    for box, label in regions:
        # Native and OCR detections can cover the same visible occurrence.
        if any(
            label == old_label
            and box[0] <= old[2]
            and old[0] <= box[2]
            and box[1] <= old[3]
            and old[1] <= box[3]
            for old, old_label in used
        ):
            continue
        selected = None
        for size in range(14, 5, -1):
            font = ImageFont.load_default(size=size)
            bounds = draw.textbbox((0, 0), label, font=font)
            if bounds[2] - bounds[0] <= box[2] - box[0] and (
                bounds[3] - bounds[1] <= box[3] - box[1]
            ):
                selected = (font, bounds)
                break
        if selected is None:
            raise JobError("label_render_overflow")
        font, bounds = selected
        draw.text((box[0] - bounds[0], box[1] - bounds[1]), label, fill="white", font=font)
        used.append((box, label))


def _text(source):
    payload = source.read_bytes()
    if payload.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16-le" if payload.startswith(codecs.BOM_UTF16_LE) else "utf-16-be"
        bom, payload = payload[:2], payload[2:]
    else:
        encoding = "utf-8"
        bom = codecs.BOM_UTF8 if payload.startswith(codecs.BOM_UTF8) else b""
        payload = payload[len(bom) :]
    try:
        text = payload.decode(encoding, errors="strict")
    except UnicodeError:
        raise JobError("unsupported_text_encoding") from None
    if "\x00" in text:
        raise JobError("unsupported_text_encoding")
    # Binary data or ZIP/PDF/image content must not be relabeled as plain text.
    if any(ord(c) < 32 and c not in "\t\r\n\f" for c in text):
        raise JobError("format_mismatch")
    return text, encoding, bom


def _workbook(source):
    from openpyxl import load_workbook

    xlsx_preflight(source)
    book = load_workbook(source, data_only=False, read_only=False, keep_links=False)
    if not 1 <= len(book.worksheets) <= MAX_SHEETS:
        book.close()
        raise JobError("workbook_sheet_limit")
    cells = []
    # Enumerate only stored cells: never materialize an enormous sparse rectangle.
    for sheet_index, sheet in enumerate(book.worksheets):
        for cell in sorted(sheet._cells.values(), key=lambda c: (c.row, c.column)):
            if cell.value is None:
                continue
            if not (1 <= cell.row <= 1_048_576 and 1 <= cell.column <= 16_384):
                raise JobError("invalid_cell_coordinate")
            if cell.data_type in ("f", "e") or not isinstance(cell.value, (str, int, float, bool)):
                raise JobError("unsupported_cell_type")
            if not isinstance(cell.value, (str, bool)) and (
                cell.number_format not in ("General", "0") or not math.isfinite(cell.value)
            ):
                raise JobError("unsupported_numeric_format")
            text = str(cell.value) if not isinstance(cell.value, bool) else ""
            if len(text) > 32767:
                raise JobError("cell_text_limit")
            cells.append(
                (
                    sheet_index,
                    cell,
                    Unit(text, {"sheet": sheet_index + 1, "row": cell.row, "column": cell.column}),
                )
            )
    return book, cells


def _pdf_page(page):
    import pymupdf
    from PIL import Image

    if math.ceil(page.rect.width * 200 / 72) * math.ceil(page.rect.height * 200 / 72) > MAX_PIXELS:
        raise JobError("image_pixel_limit")
    pixmap = page.get_pixmap(dpi=200, colorspace=pymupdf.csRGB, alpha=False, annots=True)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def _pdf_native(page, size):
    import pymupdf

    units = []
    for block_index, block in enumerate(page.get_text("rawdict")["blocks"]):
        if block.get("type") != 0:
            continue
        text, geometry = [], []
        for line in block["lines"]:
            if text:
                text.append("\n")
                geometry.append(None)
            for span in line["spans"]:
                for char in span["chars"]:
                    rect = pymupdf.Rect(char["bbox"]) * page.rotation_matrix
                    box = (
                        rect.x0 * size[0] / page.rect.width,
                        rect.y0 * size[1] / page.rect.height,
                        rect.x1 * size[0] / page.rect.width,
                        rect.y1 * size[1] / page.rect.height,
                    )
                    text.append(char["c"])
                    geometry.extend([box] * len(char["c"]))
        units.append(Unit("".join(text), {"page": page.number + 1, "block": block_index}, geometry))
    return units


def _word(source, output, mode, analyze, ocr):
    import ase_document_tools as tools

    # Reuse the legacy converter and supported Word subset, not Office cloud OCR.
    with tools._word_input(source, "soffice") as converted:
        package_files = package(converted)
        if "[Content_Types].xml" not in package_files or "_rels/.rels" not in package_files:
            raise JobError("invalid_word_package")
        types = tools._xml(package_files["[Content_Types].xml"])
        if not any(
            item.get("PartName") == "/word/document.xml"
            and item.get("ContentType") == MIME[".docx"] + ".main+xml"
            for item in types
        ):
            raise JobError("unsupported_word_content_type")
        files, stories = tools._load_word(converted)
        units, cache, image_units = [], {}, {}
        total_pixels = 0
        for name in sorted(files):
            if not name.startswith("word/media/"):
                continue
            image, _ = tools._image(files[name])
            total_pixels += image.width * image.height
            if total_pixels > MAX_TOTAL_PIXELS:
                raise JobError("total_pixel_limit")
            text, geometry = ocr(image)
            key = hashlib.sha256(image.tobytes()).digest()
            cache[key] = (text, geometry)
            image_units[name] = Unit(text, {"image": len(image_units) + 1}, geometry)
        seen_images = set()
        ordered_stories = sorted(
            stories.items(), key=lambda item: (item[0] != "word/document.xml", item[0])
        )
        for story_index, (name, root) in enumerate(ordered_stories):
            rels = tools._relationships(files, name)
            for index, paragraph in enumerate(root.iter(f"{{{tools.W}}}p")):
                text, _ = tools._paragraph_text(paragraph, visible_only=mode != "annotate")
                units.append(Unit(text, {"story": story_index + 1, "paragraph": index + 1}))
                for blip in paragraph.iter(f"{{{tools.A}}}blip"):
                    rel = rels.get(blip.get(f"{{{tools.R}}}embed"))
                    image_name = "word/" + rel.get("Target", "") if rel is not None else ""
                    if image_name in image_units and image_name not in seen_images:
                        units.append(image_units[image_name])
                        seen_images.add(image_name)
        units.extend(unit for name, unit in image_units.items() if name not in seen_images)
        detect(units, analyze)
        labels = Labels(units)
        by_text = {unit.text: unit for unit in units}

        def detected(text):
            if text not in by_text:
                raise JobError("word_text_mapping_failed")
            return by_text[text].detections

        def cached_ocr(image):
            key = hashlib.sha256(image.tobytes()).digest()
            if key not in cache:
                raise JobError("word_image_mapping_failed")
            return cache[key]

        def replacement(text, start, end):
            unit = by_text[text]
            item = next(d for d in unit.detections if d["start"] == start and d["end"] == end)
            return labels.replacement(unit, item) if mode == "sanitize" else "[REDACTED]"

        def image_transform(image, text, geometry):
            unit = Unit(text, {}, geometry, by_text[text].detections)
            paint(image, [unit], mode, labels)
            return len(by_text[text].detections)

        if mode == "annotate":
            tools.annotate_word(converted, output, detected, ocr_provider=cached_ocr)
        else:
            tools.redact_word(
                converted,
                output,
                detected,
                ocr_provider=cached_ocr,
                replacement=replacement,
                image_transform=image_transform,
            )
        return units, labels


def process(source: Path, folder: Path, mode, analyze, ocr):
    """Process one private local file into a complete, unpublished artifact set.

    :param source: Local immutable input whose extension came from a validated job.
    :param folder: Empty private output directory; never a shared cloud mount.
    :param mode: annotate, redact, or sanitize.
    :param analyze: Managed-identity ASE analyzer callable (or synthetic test double).
    :param ocr: Explicit Azure OCR callable, with no local OCR fallback.
    :return: Safe artifact manifest; source values and label maps stay in memory.
    :raises JobError: Unsupported capability, invalid input, or processing failure.
    """
    if mode not in OPERATIONS or not source.is_file() or source.stat().st_size > MAX_BYTES:
        raise JobError("invalid_document")
    if any(folder.iterdir()):
        raise JobError("output_directory_not_empty")
    suffix = source.suffix.lower()
    target_suffix = ".docx" if suffix == ".doc" else suffix
    if target_suffix not in MIME:
        raise JobError("unsupported_format")
    output = folder / ("document" + target_suffix)
    signature = source.open("rb")
    with signature:
        header = signature.read(8)
    if suffix == ".doc" and header != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise JobError("format_mismatch")
    if suffix in (".docx", ".xlsx") and not header.startswith(b"PK"):
        raise JobError("format_mismatch")
    if suffix == ".pdf" and not header.startswith(b"%PDF-"):
        raise JobError("format_mismatch")
    if suffix == ".txt":
        if header.startswith(
            (b"%PDF-", b"PK\x03\x04", b"\x89PNG", b"\xff\xd8", b"II*\x00", b"MM\x00*")
        ):
            raise JobError("format_mismatch")
        text, encoding, bom = _text(source)
        units = [Unit(text, {"text": 1})]
        detect(units, analyze)
        labels = Labels(units)
        output.write_bytes(
            source.read_bytes()
            if mode == "annotate"
            else bom + replace(units[0], mode, labels).encode(encoding)
        )
    elif suffix in (".docx", ".doc"):
        units, labels = _word(source, output, mode, analyze, ocr)
    elif suffix == ".xlsx":
        from openpyxl import Workbook
        from openpyxl.styles import PatternFill

        book, cells = _workbook(source)
        units = [unit for _, _, unit in cells]
        detect(units, analyze)
        labels = Labels(units)
        fresh = Workbook()
        fresh.remove(fresh.active)
        for index, sheet in enumerate(book.worksheets):
            new = fresh.create_sheet(sheet.title if mode == "annotate" else f"Sheet {index + 1}")
            # Hidden content is scanned and deliberately made visible in the rebuilt file.
            new.sheet_state = "visible"
        for index, cell, unit in cells:
            value = (
                replace(unit, mode, labels)
                if unit.detections and mode != "annotate"
                else cell.value
            )
            target = fresh.worksheets[index].cell(cell.row, cell.column)
            if isinstance(value, str) and len(value) > 32767:
                raise JobError("cell_text_limit")
            target.value = value
            if isinstance(value, str):
                target.data_type = "s"
            if mode == "annotate" and unit.detections:
                target.fill = PatternFill("solid", fgColor="FFFF00")
        fresh.save(output)
        book.close()
        fresh.close()
        xlsx_preflight(output)
    elif suffix == ".pdf":
        import pymupdf

        units, page_units = [], []
        total_pixels = 0
        with pymupdf.open(source) as book:
            if not book.is_pdf or book.needs_pass or not 1 <= len(book) <= MAX_FRAMES:
                raise JobError("unsupported_pdf")
            for page in book:
                image = _pdf_page(page)
                total_pixels += image.width * image.height
                if total_pixels > MAX_TOTAL_PIXELS:
                    raise JobError("total_pixel_limit")
                native = _pdf_native(page, image.size)
                text, geometry = ocr(image)
                group = native + [Unit(text, {"page": page.number + 1, "ocr": True}, geometry)]
                page_units.append(group)
                units.extend(group)
            detect(units, analyze)
            labels = Labels(units)
            if mode == "annotate":
                for page, group in zip(book, page_units):
                    image = _pdf_page(page)
                    for unit in group:
                        for item in unit.detections:
                            for box in _boxes(unit, item, image.size):
                                rect = (
                                    pymupdf.Rect(
                                        box[0] * page.rect.width / image.width,
                                        box[1] * page.rect.height / image.height,
                                        box[2] * page.rect.width / image.width,
                                        box[3] * page.rect.height / image.height,
                                    )
                                    * page.derotation_matrix
                                )
                                annotation = page.add_rect_annot(rect)
                                annotation.set_colors(stroke=(1, 1, 0))
                                annotation.update()
                book.save(output)
            else:
                with pymupdf.open() as fresh:
                    for page, group in zip(book, page_units):
                        image = _pdf_page(page)
                        paint(image, group, mode, labels)
                        stream = io.BytesIO()
                        image.save(stream, format="PNG")
                        new = fresh.new_page(width=page.rect.width, height=page.rect.height)
                        new.insert_image(new.rect, stream=stream.getvalue())
                    fresh.save(output, garbage=4, deflate=True)
    else:
        from PIL import Image

        expected = {
            ".png": "PNG",
            ".jpg": "JPEG",
            ".jpeg": "JPEG",
            ".tif": "TIFF",
            ".tiff": "TIFF",
        }[suffix]
        units, total_pixels, frame_files = [], 0, []
        with Image.open(source) as original:
            frames = getattr(original, "n_frames", 1)
            if original.format != expected or not 1 <= frames <= MAX_FRAMES:
                raise JobError("format_mismatch")
            if frames != 1 and expected != "TIFF":
                raise JobError("unsupported_animation")
            for index in range(frames):
                original.seek(index)
                image = _image_rgb(original)
                total_pixels += image.width * image.height
                if total_pixels > MAX_TOTAL_PIXELS:
                    raise JobError("total_pixel_limit")
                text, geometry = ocr(image)
                units.append(Unit(text, {"frame": index + 1}, geometry))
            detect(units, analyze)
            labels = Labels(units)
            for index, unit in enumerate(units):
                original.seek(index)
                image = _image_rgb(original)
                paint(image, [unit], mode, labels)
                # Stage TIFF frames on disk rather than holding all decoded pixels in memory.
                if expected == "TIFF":
                    frame_path = folder / f"frame-{index}.tiff"
                    image.save(frame_path, format="TIFF", compression="tiff_deflate")
                    frame_files.append(frame_path)
                else:
                    image.save(output, format=expected)
        if frame_files:
            from contextlib import ExitStack

            with ExitStack() as stack:
                images = [stack.enter_context(Image.open(path)) for path in frame_files]
                images[0].save(
                    output, save_all=True, append_images=images[1:], compression="tiff_deflate"
                )
            for path in frame_files:
                path.unlink()
    artifacts = [{"name": output.name, "content_type": MIME[target_suffix]}]
    if mode == "annotate":
        sidecar = folder / "annotations.json"
        sidecar.write_text(
            json.dumps({"version": 1, "annotations": annotations(units)}), encoding="utf-8"
        )
        artifacts.append({"name": sidecar.name, "content_type": "application/json"})
    manifest = {
        "version": 1,
        "policy": POLICY,
        "handler_version": "0.1.0",
        "operation": mode,
        "sensitive": mode == "annotate",
        "warnings": labels.warnings if mode == "sanitize" else [],
        "artifacts": artifacts,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest
