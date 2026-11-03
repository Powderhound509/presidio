"""Document workflows for the ASE samples; no local Presidio engines."""

import copy
import csv
import io
import math
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from xml.sax.saxutils import quoteattr


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS = {"w": W, "r": R, "a": A, "wp": WP}
MAX_BYTES = 100 * 1024 * 1024
MAX_PIXELS = 25_000_000


class ASEAnalyzer:
    """Managed-identity REST analyzer with privacy-safe errors.

    :param url: Root HTTPS analyzer URL.
    :param resource: Registered API resource, without /.default.
    :param client_id: Calling user-assigned identity client ID, or empty.
    :param language: ISO 639-1 detection language.
    :raises ValueError: Invalid service URL or resource.
    """

    def __init__(self, url, resource, client_id="", language="en"):
        import requests
        from azure.identity import ManagedIdentityCredential

        parsed = urlsplit(url.rstrip("/"))
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or parsed.port not in (None, 443)
        ):
            raise ValueError("Configure a root HTTPS analyzer URL.")
        if not resource.strip() or resource.rstrip("/").endswith("/.default"):
            raise ValueError("Set ANALYZER_API_RESOURCE to the registered URI without /.default.")
        self.url = url.rstrip("/")
        self.resource = resource.strip()
        self.language = language
        self.credential = (
            ManagedIdentityCredential(client_id=client_id.strip())
            if client_id.strip() else ManagedIdentityCredential()
        )
        self.session = requests.Session()
        self.session.auth = lambda request: request

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.session.close()
        self.credential.close()

    def analyze(self, text):
        """Analyze exact text without logging input, tokens, or HTTP bodies.

        :param text: Original text, without normalization.
        :return: Validated detection dictionaries.
        :raises RuntimeError: Authentication, transport, HTTP, or response failure.
        :raises ValueError: Text block exceeds the sample size limit.
        """
        import requests
        from azure.core.exceptions import AzureError

        if not text:
            return []
        if len(text) > 100_000:
            raise ValueError("Text block exceeds this sample's 100,000-character limit.")
        try:
            token = self.credential.get_token(self.resource.rstrip("/") + "/.default")
        except (AzureError, ValueError):
            raise RuntimeError("Token acquisition failed; check the VM identity and API resource.") from None
        try:
            response = self.session.post(
                self.url + "/analyze",
                headers={"Authorization": "Bearer " + token.token},
                json={"text": text, "language": self.language},
                timeout=(10, 60), allow_redirects=False, verify=True,
            )
        except requests.exceptions.RequestException:
            raise RuntimeError("Analyzer transport failed; check DNS, routing, TLS, and service health.") from None
        if response.status_code != 200:
            raise RuntimeError(f"Analyzer returned HTTP {response.status_code}; body omitted for privacy.")
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError("Analyzer returned invalid JSON.") from None
        return _detections(data, text)


def _detections(data, text):
    if not isinstance(data, list):
        raise RuntimeError("Analyzer response must be a list.")
    for item in data:
        if not isinstance(item, dict):
            raise RuntimeError("Detection must be an object.")
        start, end = item.get("start"), item.get("end")
        score, entity = item.get("score"), item.get("entity_type")
        if (
            type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(text)
            or type(score) not in (int, float) or not 0 <= score <= 1
            or not math.isfinite(score)
            or not isinstance(entity, str) or not entity.strip()
        ):
            raise RuntimeError("Analyzer returned invalid offsets, score, or entity type.")
    return data


def _intervals(detections, text):
    spans = sorted((d["start"], d["end"]) for d in _detections(detections, text))
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _paths(source, target, suffix):
    source, target = Path(source).resolve(), Path(target).resolve()
    if not source.is_file() or source.stat().st_size > MAX_BYTES:
        raise ValueError("Input must exist and be no larger than 100 MiB.")
    if target.suffix.lower() != suffix:
        raise ValueError(f"Output must use the {suffix} extension.")
    if source == target or target.exists():
        raise FileExistsError("Choose a new output file; overwriting is refused.")
    if not target.parent.is_dir():
        raise FileNotFoundError("Output parent directory must exist.")
    return source, target


@contextmanager
def _new_output(target):
    # A same-directory hard link publishes atomically without replacing another file.
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        yield temporary
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _publish(payload, target):
    with _new_output(target) as temporary:
        temporary.write_bytes(payload)


@contextmanager
def _word_input(source, libreoffice):
    if source.suffix.lower() == ".docx":
        yield source
        return
    if source.suffix.lower() != ".doc":
        raise ValueError("Word input must be .docx or legacy .doc.")
    executable = shutil.which(libreoffice)
    if not executable:
        raise FileNotFoundError("Install LibreOffice on the kernel host and set LIBREOFFICE_COMMAND.")
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        profile = root / "profile"
        profile.mkdir()
        (profile / "user").mkdir()
        (profile / "user" / "registrymodifications.xcu").write_text(
            '<?xml version="1.0"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            '</item></oor:items>',
            encoding="utf-8",
        )
        try:
            result = subprocess.run(
                [executable, f"-env:UserInstallation={profile.as_uri()}", "--headless",
                 "--convert-to", "docx:Office Open XML Text", "--outdir", str(root),
                 str(source)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Legacy DOC conversion timed out.") from None
        converted = root / (source.stem + ".docx")
        if result.returncode != 0 or not converted.is_file():
            raise RuntimeError("Legacy DOC conversion failed; converter output omitted for privacy.")
        yield converted


def _xml(payload):
    markup = payload.replace(b"\0", b"").upper()
    if b"<!DOCTYPE" in markup or b"<!ENTITY" in markup:
        raise ValueError("DTD/entity declarations are unsupported.")
    return ET.fromstring(payload)


def _load_word(source):
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if len(infos) > 1000 or sum(item.file_size for item in infos) > MAX_BYTES:
            raise ValueError("DOCX package exceeds the sample's expansion limits.")
        if len({item.filename for item in infos}) != len(infos):
            raise ValueError("DOCX contains duplicate package entries.")
        if any(
            "\\" in item.filename or item.filename.startswith("/")
            or ".." in Path(item.filename).parts
            for item in infos
        ):
            raise ValueError("DOCX contains unsafe package paths.")
        files = {item.filename: archive.read(item) for item in infos}
    if "word/document.xml" not in files:
        raise ValueError("Input is not a supported DOCX package.")
    if any(
        name.startswith(("word/embeddings/", "word/charts/", "word/diagrams/"))
        or name.lower().endswith("vbaproject.bin")
        or name in ("word/footnotes.xml", "word/endnotes.xml")
        for name in files
    ):
        raise ValueError("Embedded objects, macros, charts, diagrams, and footnotes/endnotes are unsupported.")
    stories = {
        name: _xml(payload) for name, payload in files.items()
        if name == "word/document.xml"
        or (name.startswith(("word/header", "word/footer")) and name.endswith(".xml"))
    }
    forbidden = {
        f"{{{W}}}{name}" for name in (
            "ins", "del", "moveFrom", "moveTo", "fldChar", "instrText", "fldSimple",
            "txbxContent", "sdt", "object", "altChunk", "sym",
            "pict", "subDoc", "ruby", "contentPart", "footnoteReference", "endnoteReference",
        )
    }
    forbidden.add(f"{{{WP}}}anchor")
    forbidden.add(f"{{{A}}}srcRect")
    for root in stories.values():
        for node in root.iter():
            if node.tag in forbidden or "officeDocument/2006/math" in node.tag:
                raise ValueError("Tracked changes, fields, text boxes, controls, symbols, floating drawings, or equations are unsupported.")
            if node.tag == f"{{{A}}}graphicData" and node.get("uri") != (
                "http://schemas.openxmlformats.org/drawingml/2006/picture"
            ):
                raise ValueError("Only inline raster pictures are supported.")
            if node.tag == f"{{{A}}}xfrm" and any(
                node.get(key, "0") not in ("0", "false", "off")
                for key in ("rot", "flipH", "flipV")
            ):
                raise ValueError("Cropped, rotated, or flipped Word pictures are unsupported.")
            if node.tag == f"{{{A}}}blip" and len(node):
                raise ValueError("Picture effects are unsupported.")
            if node.tag == f"{{{W}}}r" and any(
                child.tag not in {f"{{{W}}}{tag}" for tag in (
                    "rPr", "t", "tab", "br", "cr", "noBreakHyphen", "softHyphen",
                    "drawing", "lastRenderedPageBreak",
                    "commentReference", "annotationRef",
                )}
                for child in node
            ):
                raise ValueError("Unsupported Word run content.")
        if root.findall(".//w:pPr/w:sectPr", NS):
            raise ValueError("Multiple Word sections are unsupported; split the document first.")
    for name, payload in files.items():
        if name.endswith(".rels"):
            for rel in _xml(payload):
                if rel.get("TargetMode") == "External" and not rel.get("Type", "").endswith("/hyperlink"):
                    raise ValueError("External linked content is unsupported.")
    return files, stories


def _relationships(files, story):
    name = str(Path(story).parent / "_rels" / (Path(story).name + ".rels")).replace("\\", "/")
    return {r.get("Id"): r for r in _xml(files[name])} if name in files else {}


def _hidden(run):
    return any(
        prop.get(f"{{{W}}}val", "true") not in ("false", "0", "off")
        for tag in ("vanish", "webHidden", "specVanish")
        for prop in run.findall(f"w:rPr/w:{tag}", NS)
    )


def _flag(node):
    if node is None:
        return False
    value = node.get(f"{{{W}}}val", "true")
    if value not in ("true", "false", "on", "off", "1", "0"):
        raise ValueError("Invalid Word boolean property.")
    return value in ("true", "on", "1")


def _paragraph_text(paragraph, *, visible_only=False):
    parts, entries = [], []
    for run in paragraph.iter(f"{{{W}}}r"):
        if visible_only and _hidden(run):
            continue
        for child in run:
            tag = child.tag.rsplit("}", 1)[-1]
            text = {
                "tab": "\t", "br": "\n", "cr": "\n",
                "noBreakHyphen": "\u2011", "softHyphen": "\u00ad",
            }.get(tag, child.text or "" if tag == "t" else "")
            if text:
                parts.append(text)
                entries.append((run, child, text))
    return "".join(parts), entries


def _mark_paragraph(paragraph, analyze):
    text, entries = _paragraph_text(paragraph)
    spans = _intervals(analyze(text), text) if text else []
    parents = {child: parent for parent in paragraph.iter() for child in parent}
    offset, span_index = 0, 0
    for run, child, value in entries:
        marked = []
        for i in range(len(value)):
            position = offset + i
            while span_index < len(spans) and spans[span_index][1] <= position:
                span_index += 1
            marked.append(span_index < len(spans) and spans[span_index][0] <= position)
        offset += len(value)
        if not any(marked):
            continue
        # Split text children into independent runs, keeping their original properties.
        parent = parents[run]
        insertion = list(parent).index(run)
        before = list(run).index(child)
        prefix = ET.Element(f"{{{W}}}r", run.attrib)
        properties = run.find("w:rPr", NS)
        if properties is not None:
            prefix.append(copy.deepcopy(properties))
        for element in list(run)[:before]:
            if element.tag != f"{{{W}}}rPr":
                prefix.append(copy.deepcopy(element))
        if len(prefix) > (1 if properties is not None else 0):
            parent.insert(insertion, prefix)
            insertion += 1
        cuts = [0] + [i for i in range(1, len(value)) if marked[i] != marked[i - 1]] + [len(value)]
        for start, end in zip(cuts, cuts[1:]):
            new_run = ET.Element(f"{{{W}}}r", run.attrib)
            props = copy.deepcopy(properties) if properties is not None else ET.Element(f"{{{W}}}rPr")
            if marked[start]:
                for old in list(props):
                    if old.tag == f"{{{W}}}highlight":
                        props.remove(old)
                ET.SubElement(props, f"{{{W}}}highlight", {f"{{{W}}}val": "yellow"})
            new_run.append(props)
            piece = copy.deepcopy(child)
            if piece.tag == f"{{{W}}}t":
                piece.text = value[start:end]
                piece.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            new_run.append(piece)
            parent.insert(insertion, new_run)
            insertion += 1
        for element in list(run)[:before + 1]:
            if element.tag != f"{{{W}}}rPr":
                run.remove(element)
    return len(spans)


def _ocr(image, *, language, command, min_confidence):
    if not 0 <= min_confidence <= 100:
        raise ValueError("OCR_MIN_CONFIDENCE must be between 0 and 100.")
    executable = shutil.which(command)
    if not executable:
        raise FileNotFoundError("Install Tesseract and language data on the kernel host; set TESSERACT_COMMAND.")
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    try:
        result = subprocess.run(
            [executable, "stdin", "stdout", "-l", language, "tsv"],
            input=encoded.getvalue(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("OCR timed out; output refused.") from None
    if result.returncode != 0:
        raise RuntimeError("OCR failed; check installed language data. Engine output omitted for privacy.")
    rows = list(csv.DictReader(io.StringIO(result.stdout.decode("utf-8")), delimiter="\t"))
    parts, geometry, last_line = [], [], None
    for row in rows:
        word = row["text"]
        if not word.strip():
            continue
        confidence = float(row["conf"])
        if not math.isfinite(confidence) or not 0 <= confidence <= 100 or confidence < min_confidence:
            raise RuntimeError("OCR returned low-confidence text; output refused pending review.")
        line = tuple(row[key] for key in ("block_num", "par_num", "line_num"))
        if parts:
            separator = " " if line == last_line else "\n"
            parts.append(separator)
            geometry.append(None)
        x, y = int(row["left"]), int(row["top"])
        width, height = int(row["width"]), int(row["height"])
        if not 0 <= x < x + width <= image.width or not 0 <= y < y + height <= image.height:
            raise RuntimeError("OCR returned invalid image coordinates.")
        parts.append(word)
        geometry.extend([(x, y, x + width, y + height)] * len(word))
        last_line = line
    if not parts and any(low != high for low, high in image.getextrema()):
        raise RuntimeError("OCR found no text on a non-uniform image; output refused pending review.")
    return "".join(parts), geometry


def _image(payload):
    from PIL import Image

    with Image.open(io.BytesIO(payload)) as original:
        if original.format not in ("PNG", "JPEG") or getattr(original, "n_frames", 1) != 1:
            raise ValueError("Word pictures must be single-frame PNG or JPEG; vector/multi-frame images are unsupported.")
        if original.width * original.height > MAX_PIXELS:
            raise ValueError("Image exceeds the pixel limit.")
        rgba = original.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        return Image.alpha_composite(background, rgba).convert("RGB"), original.format


def _paint(image, text, geometry, analyze, *, redact):
    from PIL import ImageDraw

    if len(geometry) != len(text):
        raise RuntimeError("Text and image geometry do not align; output refused.")
    spans = _intervals(analyze(text), text) if text else []
    draw = ImageDraw.Draw(image)
    for start, end in spans:
        boxes = {entry for entry in geometry[start:end] if entry is not None}
        if not boxes:
            raise RuntimeError("A detection has no visible geometry; output refused.")
        for x0, y0, x1, y1 in boxes:
            if (
                not all(math.isfinite(value) for value in (x0, y0, x1, y1))
                or x0 >= x1 or y0 >= y1
                or x1 <= 0 or y1 <= 0 or x0 >= image.width or y0 >= image.height
            ):
                raise RuntimeError("Invalid redaction geometry; output refused.")
            box = (
                max(0, math.floor(x0) - 2), max(0, math.floor(y0) - 2),
                min(image.width - 1, math.ceil(x1) + 2),
                min(image.height - 1, math.ceil(y1) + 2),
            )
            if redact:
                draw.rectangle(box, fill="black")
            else:
                draw.rectangle(box, outline="yellow", width=3)
    return len(spans)


def _word_images(files, analyze, *, redact, ocr_language, tesseract, min_confidence):
    count = 0
    for name in files:
        if name.startswith("word/media/"):
            image, format_name = _image(files[name])
            text, geometry = _ocr(
                image, language=ocr_language, command=tesseract,
                min_confidence=min_confidence,
            )
            count += _paint(image, text, geometry, analyze, redact=redact)
            stream = io.BytesIO()
            image.save(stream, format="PNG" if redact else format_name)
            files[name] = stream.getvalue()
    return count


def _zip(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return stream.getvalue()


def _story_xml(root, original):
    # Compatibility attributes contain literal prefixes that ElementTree does not remap.
    aliases = {}
    for event, (prefix, uri) in ET.iterparse(io.BytesIO(original), events=("start-ns",)):
        if prefix in aliases and aliases[prefix] != uri:
            raise ValueError("Rebound XML namespace prefixes are unsupported.")
        aliases[prefix] = uri
    payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    beginning = payload.index(b"<", payload.index(b"?>") + 2)
    end = payload.index(b">", beginning)
    tag = payload[beginning:end]
    declarations = []
    for prefix, uri in aliases.items():
        if prefix and prefix != "xml" and f"xmlns:{prefix}=".encode() not in tag:
            declarations.append(f" xmlns:{prefix}={quoteattr(uri)}".encode())
    return payload[:end] + b"".join(declarations) + payload[end:]


def annotate_word(source, target, analyze, *, libreoffice="soffice", ocr_language="eng",
                  tesseract="tesseract", min_confidence=50):
    """Highlight Word text and OCR-detected text in embedded raster images.

    :param source: DOCX or legacy DOC input.
    :param target: New DOCX output.
    :param analyze: Callable using the ASE analyzer.
    :param libreoffice: LibreOffice executable for legacy DOC conversion.
    :param ocr_language: Installed Tesseract language code(s).
    :param tesseract: Tesseract executable.
    :param min_confidence: Minimum OCR word confidence, otherwise refuse output.
    :return: Counts only, never input text.
    :raises ValueError: Unsupported input features.
    :raises RuntimeError: Analysis, OCR, or conversion failure.
    """
    source, target = _paths(source, target, ".docx")
    with _word_input(source, libreoffice) as converted:
        files, stories = _load_word(converted)
        text_count = sum(
            _mark_paragraph(paragraph, analyze)
            for root in stories.values() for paragraph in root.iter(f"{{{W}}}p")
        )
        image_count = _word_images(
            files, analyze, redact=False, ocr_language=ocr_language,
            tesseract=tesseract, min_confidence=min_confidence,
        )
        for name, root in stories.items():
            files[name] = _story_xml(root, files[name])
        _publish(_zip(files), target)
    return {"text_detections": text_count, "image_detections": image_count}


def _clean_paragraph(paragraph, analyze, picture):
    text, entries = _paragraph_text(paragraph, visible_only=True)
    spans = _intervals(analyze(text), text) if text else []
    result = ET.Element(f"{{{W}}}p")
    offset, span_index = 0, 0
    for run in paragraph.iter(f"{{{W}}}r"):
        if _hidden(run):
            continue
        properties = run.find("w:rPr", NS)
        for child in run:
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "drawing":
                result.append(picture(child))
                continue
            value = {
                "tab": "\t", "br": "\n", "cr": "\n",
                "noBreakHyphen": "\u2011", "softHyphen": "\u00ad",
            }.get(tag, child.text or "" if tag == "t" else "")
            if not value:
                continue
            pieces = []
            for i, character in enumerate(value):
                position = offset + i
                while span_index < len(spans) and spans[span_index][1] <= position:
                    span_index += 1
                containing = (
                    spans[span_index]
                    if span_index < len(spans) and spans[span_index][0] <= position
                    else None
                )
                if containing is None:
                    pieces.append(character)
                elif position == containing[0]:
                    pieces.append("[REDACTED]")
            offset += len(value)
            if not pieces:
                continue
            fresh = ET.SubElement(result, f"{{{W}}}r")
            if properties is not None:
                clean = ET.SubElement(fresh, f"{{{W}}}rPr")
                for prop in properties:
                    if prop.tag in {f"{{{W}}}b", f"{{{W}}}i"}:
                        flag = prop.get(f"{{{W}}}val", "true")
                        if flag not in ("true", "false", "0", "1", "on", "off"):
                            raise ValueError("Invalid basic text formatting.")
                        ET.SubElement(clean, prop.tag, {f"{{{W}}}val": flag})
            for piece in re.split("([\t\n])", "".join(pieces)):
                if piece in ("\t", "\n"):
                    ET.SubElement(fresh, f"{{{W}}}tab" if piece == "\t" else f"{{{W}}}br")
                elif piece:
                    text_node = ET.SubElement(fresh, f"{{{W}}}t", {
                        "{http://www.w3.org/XML/1998/namespace}space": "preserve",
                    })
                    text_node.text = piece
    return result, len(spans)


def _picture_xml(relationship_id, width, height, picture_id):
    pic = "http://schemas.openxmlformats.org/drawingml/2006/picture"
    run = ET.Element(f"{{{W}}}r")
    drawing = ET.SubElement(run, f"{{{W}}}drawing")
    inline = ET.SubElement(drawing, f"{{{WP}}}inline")
    ET.SubElement(inline, f"{{{WP}}}extent", {"cx": width, "cy": height})
    ET.SubElement(inline, f"{{{WP}}}docPr", {"id": str(picture_id), "name": "Sanitized image"})
    graphic = ET.SubElement(inline, f"{{{A}}}graphic")
    data = ET.SubElement(graphic, f"{{{A}}}graphicData", {"uri": pic})
    picture = ET.SubElement(data, f"{{{pic}}}pic")
    nonvisual = ET.SubElement(picture, f"{{{pic}}}nvPicPr")
    ET.SubElement(nonvisual, f"{{{pic}}}cNvPr", {"id": "0", "name": "Sanitized image"})
    ET.SubElement(nonvisual, f"{{{pic}}}cNvPicPr")
    fill = ET.SubElement(picture, f"{{{pic}}}blipFill")
    ET.SubElement(fill, f"{{{A}}}blip", {f"{{{R}}}embed": relationship_id})
    stretch = ET.SubElement(fill, f"{{{A}}}stretch")
    ET.SubElement(stretch, f"{{{A}}}fillRect")
    properties = ET.SubElement(picture, f"{{{pic}}}spPr")
    transform = ET.SubElement(properties, f"{{{A}}}xfrm")
    ET.SubElement(transform, f"{{{A}}}off", {"x": "0", "y": "0"})
    ET.SubElement(transform, f"{{{A}}}ext", {"cx": width, "cy": height})
    geometry = ET.SubElement(properties, f"{{{A}}}prstGeom", {"prst": "rect"})
    ET.SubElement(geometry, f"{{{A}}}avLst")
    return run


def redact_word(source, target, analyze, *, libreoffice="soffice", ocr_language="eng",
                tesseract="tesseract", min_confidence=50):
    """Rebuild a fresh DOCX with replaced text and OCR-scrubbed pictures.

    :param source: DOCX or legacy DOC input.
    :param target: New DOCX output; never overwrites input or existing output.
    :param analyze: ASE analyzer callable.
    :param libreoffice: LibreOffice executable for DOC conversion.
    :param ocr_language: Installed Tesseract language code(s).
    :param tesseract: Tesseract executable.
    :param min_confidence: Minimum OCR word confidence.
    :return: Text/image detection counts only.
    :raises ValueError: Unsupported document features.
    :raises RuntimeError: Conversion, OCR, analysis, or geometry failure.
    """
    source, target = _paths(source, target, ".docx")
    with _word_input(source, libreoffice) as converted:
        files, stories = _load_word(converted)
        if "word/styles.xml" in files and any(
            node.tag in {f"{{{W}}}{tag}" for tag in ("vanish", "webHidden", "specVanish")}
            for node in _xml(files["word/styles.xml"]).iter()
        ):
            raise ValueError("Style-based hidden text is unsupported; output refused.")
        source_document = stories["word/document.xml"]
        old_section = source_document.find("w:body/w:sectPr", NS)
        original_rels = _relationships(files, "word/document.xml")
        settings = _xml(files["word/settings.xml"]) if "word/settings.xml" in files else None
        even_headers = _flag(settings.find("w:evenAndOddHeaders", NS)) if settings is not None else False
        first_header = _flag(old_section.find("w:titlePg", NS)) if old_section is not None else False
        story_names = {"word/document.xml": "word/document.xml"}
        references = []
        if old_section is not None:
            for child in old_section:
                if child.tag in (f"{{{W}}}headerReference", f"{{{W}}}footerReference"):
                    kind = "header" if child.tag.endswith("headerReference") else "footer"
                    rel = original_rels.get(child.get(f"{{{R}}}id"))
                    if rel is None or not rel.get("Type", "").endswith("/" + kind):
                        raise ValueError("Invalid header/footer relationship.")
                    original_name = "word/" + rel.get("Target", "")
                    if original_name not in stories:
                        raise ValueError("Header/footer data is missing.")
                    reference_type = child.get(f"{{{W}}}type", "default")
                    if reference_type not in ("default", "first", "even"):
                        raise ValueError("Invalid header/footer type.")
                    if (reference_type == "first" and not first_header) or (
                        reference_type == "even" and not even_headers
                    ):
                        continue
                    if original_name not in story_names:
                        story_names[original_name] = f"word/{kind}{len(story_names)}.xml"
                    references.append((kind, original_name, reference_type))
        stories = {name: stories[name] for name in story_names}
        image_count = _word_images(
            files, analyze, redact=True, ocr_language=ocr_language,
            tesseract=tesseract, min_confidence=min_confidence,
        )
        output = {}
        content_types = ET.Element(f"{{{CT}}}Types")
        ET.SubElement(content_types, f"{{{CT}}}Default", {"Extension": "rels", "ContentType": "application/vnd.openxmlformats-package.relationships+xml"})
        ET.SubElement(content_types, f"{{{CT}}}Default", {"Extension": "xml", "ContentType": "application/xml"})
        document_rels = ET.Element(f"{{{PKG}}}Relationships")
        text_count, picture_count = 0, 0
        rebuilt = {}
        for story, root in stories.items():
            rels = _relationships(files, story)
            new_rels = document_rels if story == "word/document.xml" else ET.Element(f"{{{PKG}}}Relationships")

            def picture(drawing):
                nonlocal picture_count
                blips = list(drawing.iter(f"{{{A}}}blip"))
                extent = drawing.find(".//wp:extent", NS)
                if len(blips) != 1 or extent is None:
                    raise ValueError("Only ordinary inline pictures are supported.")
                if any(
                    not (extent.get(key, "").isascii() and extent.get(key, "").isdigit())
                    or not 0 < int(extent.get(key, "0")) <= 1_000_000_000
                    for key in ("cx", "cy")
                ):
                    raise ValueError("Invalid inline picture dimensions.")
                rel = rels.get(blips[0].get(f"{{{R}}}embed"))
                if rel is None or not rel.get("Type", "").endswith("/image"):
                    raise ValueError("Picture has an invalid image relationship.")
                target_name = rel.get("Target", "")
                if not target_name.startswith("media/") or ".." in target_name.split("/"):
                    raise ValueError("Unsupported picture target.")
                original_name = "word/" + target_name
                if original_name not in files:
                    raise ValueError("Picture data is missing.")
                image, _ = _image(files[original_name])
                stream = io.BytesIO()
                image.save(stream, format="PNG")
                picture_count += 1
                media = f"media/image{picture_count}.png"
                output["word/" + media] = stream.getvalue()
                rel_id = f"picture{picture_count}"
                ET.SubElement(new_rels, f"{{{PKG}}}Relationship", {
                    "Id": rel_id, "Type": R + "/image", "Target": media,
                })
                return _picture_xml(rel_id, extent.get("cx"), extent.get("cy"), picture_count)

            def block(node):
                nonlocal text_count
                if node.tag == f"{{{W}}}p":
                    clean, count = _clean_paragraph(node, analyze, picture)
                    text_count += count
                    return clean
                if node.tag == f"{{{W}}}tbl":
                    table = ET.Element(node.tag)
                    for child in node:
                        if child.tag == f"{{{W}}}tblGrid":
                            grid = ET.SubElement(table, child.tag)
                            for column in child:
                                width = column.get(f"{{{W}}}w", "")
                                if column.tag != f"{{{W}}}gridCol" or not width.isascii() or not width.isdigit():
                                    raise ValueError("Unsupported table grid.")
                                ET.SubElement(grid, column.tag, {f"{{{W}}}w": width})
                        elif child.tag == f"{{{W}}}tr":
                            row = ET.SubElement(table, child.tag)
                            for cell in child.findall("w:tc", NS):
                                fresh = ET.SubElement(row, cell.tag)
                                props = cell.find("w:tcPr", NS)
                                if props is not None:
                                    clean = ET.SubElement(fresh, props.tag)
                                    for prop in props:
                                        if prop.tag == f"{{{W}}}gridSpan":
                                            width = prop.get(f"{{{W}}}val", "")
                                            if not width.isascii() or not width.isdigit():
                                                raise ValueError("Invalid table span.")
                                            ET.SubElement(clean, prop.tag, {f"{{{W}}}val": width})
                                        elif prop.tag == f"{{{W}}}vMerge":
                                            flag = prop.get(f"{{{W}}}val", "continue")
                                            if flag not in ("restart", "continue"):
                                                raise ValueError("Invalid table merge.")
                                            ET.SubElement(clean, prop.tag, {f"{{{W}}}val": flag})
                                for item in cell:
                                    if item.tag in (f"{{{W}}}p", f"{{{W}}}tbl"):
                                        fresh.append(block(item))
                    return table
                raise ValueError("Unsupported Word block; output refused.")

            clean_root = ET.Element(root.tag)
            container = root.find("w:body", NS) if story == "word/document.xml" else root
            destination = ET.SubElement(clean_root, f"{{{W}}}body") if story == "word/document.xml" else clean_root
            for child in container:
                if child.tag in (f"{{{W}}}p", f"{{{W}}}tbl"):
                    destination.append(block(child))
                elif child.tag != f"{{{W}}}sectPr":
                    raise ValueError("Unsupported Word body content.")
            rebuilt[story_names[story]] = clean_root
            if story != "word/document.xml":
                relationship_file = "word/_rels/" + Path(story_names[story]).name + ".rels"
                output[relationship_file] = ET.tostring(new_rels, encoding="utf-8")
        body = rebuilt["word/document.xml"].find("w:body", NS)
        section = ET.SubElement(body, f"{{{W}}}sectPr")
        for index, (kind, original_name, reference_type) in enumerate(references):
            rel_id = f"story{index}"
            ET.SubElement(document_rels, f"{{{PKG}}}Relationship", {
                "Id": rel_id, "Type": R + "/" + kind,
                "Target": Path(story_names[original_name]).name,
            })
            ET.SubElement(section, f"{{{W}}}{kind}Reference", {
                f"{{{W}}}id": rel_id, f"{{{W}}}type": reference_type,
            })
        if first_header:
            ET.SubElement(section, f"{{{W}}}titlePg")
        if even_headers:
            settings = ET.Element(f"{{{W}}}settings")
            ET.SubElement(settings, f"{{{W}}}evenAndOddHeaders")
            output["word/settings.xml"] = ET.tostring(settings, encoding="utf-8")
            ET.SubElement(document_rels, f"{{{PKG}}}Relationship", {
                "Id": "settings", "Type": R + "/settings", "Target": "settings.xml",
            })
            ET.SubElement(content_types, f"{{{CT}}}Override", {
                "PartName": "/word/settings.xml",
                "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml",
            })
        for story, root in rebuilt.items():
            output[story] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            kind = "document.main" if story == "word/document.xml" else ("header" if story.startswith("word/header") else "footer")
            ET.SubElement(content_types, f"{{{CT}}}Override", {
                "PartName": "/" + story,
                "ContentType": f"application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}+xml",
            })
        ET.SubElement(content_types, f"{{{CT}}}Default", {"Extension": "png", "ContentType": "image/png"})
        package_rels = ET.Element(f"{{{PKG}}}Relationships")
        ET.SubElement(package_rels, f"{{{PKG}}}Relationship", {
            "Id": "document", "Type": R + "/officeDocument", "Target": "word/document.xml",
        })
        output["_rels/.rels"] = ET.tostring(package_rels, encoding="utf-8")
        output["word/_rels/document.xml.rels"] = ET.tostring(document_rels, encoding="utf-8")
        output["[Content_Types].xml"] = ET.tostring(content_types, encoding="utf-8")
        _publish(_zip(output), target)
    return {"text_detections": text_count, "image_detections": image_count}


def redact_pdf(source, target, analyze, *, dpi=200, ocr_language="eng",
               tesseract="tesseract", min_confidence=50):
    """Rebuild image-only PDF pages after native-text and whole-page OCR detection.

    :param source: PDF input, including scanned pages.
    :param target: New PDF output.
    :param analyze: ASE analyzer callable.
    :param dpi: Rendering resolution, at least 144 DPI.
    :param ocr_language: Installed Tesseract language code(s).
    :param tesseract: Tesseract executable.
    :param min_confidence: Minimum OCR word confidence.
    :return: Page and detection counts only.
    :raises ValueError: Unsupported input or size.
    :raises RuntimeError: OCR, detection, or geometry failure.
    """
    import pymupdf
    from PIL import Image

    source, target = _paths(source, target, ".pdf")
    if source.suffix.lower() != ".pdf" or type(dpi) is not int or dpi < 144:
        raise ValueError("Use PDF input and an integer resolution of at least 144 DPI.")
    text_count, image_count = 0, 0
    with pymupdf.open(source) as original, pymupdf.open() as output:
        if not original.is_pdf or original.needs_pass or not 1 <= len(original) <= 200:
            raise ValueError("Encrypted, empty, or more-than-200-page PDFs are unsupported.")
        for page in original:
            if math.ceil(page.rect.width * dpi / 72) * math.ceil(page.rect.height * dpi / 72) > MAX_PIXELS:
                raise ValueError("Rendered page exceeds the pixel limit; reduce DPI or split the document.")
            pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False, annots=True)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            text, geometry = _ocr(
                image, language=ocr_language, command=tesseract,
                min_confidence=min_confidence,
            )
            image_count += _paint(image, text, geometry, analyze, redact=True)
            for block in page.get_text("rawdict")["blocks"]:
                if block.get("type") != 0:
                    continue
                parts, boxes = [], []
                for line in block["lines"]:
                    if parts:
                        parts.append("\n")
                        boxes.append(None)
                    for span in line["spans"]:
                        for char in span["chars"]:
                            parts.append(char["c"])
                            rect = pymupdf.Rect(char["bbox"]) * page.rotation_matrix
                            box = (
                                rect.x0 * image.width / page.rect.width,
                                rect.y0 * image.height / page.rect.height,
                                rect.x1 * image.width / page.rect.width,
                                rect.y1 * image.height / page.rect.height,
                            )
                            boxes.extend([box] * len(char["c"]))
                text_count += _paint(image, "".join(parts), boxes, analyze, redact=True)
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            clean_page = output.new_page(width=page.rect.width, height=page.rect.height)
            clean_page.insert_image(clean_page.rect, stream=stream.getvalue())
        with _new_output(target) as temporary:
            output.save(str(temporary), garbage=4, deflate=True)
        return {"pages": len(original), "text_detections": text_count, "ocr_detections": image_count}
