"""Bounded Document Intelligence Read REST adapter for Azure Government."""

import io
import math
import time
from urllib.parse import urlsplit

from .contract import MAX_CHARACTERS, MAX_PIXELS, JobError


def normalize(result, size, minimum):
    """Validate single-raster Read output into Python code points and pixel boxes.

    :param result: Read result requested with unicodeCodePoint indexing.
    :param size: Exact submitted image dimensions.
    :param minimum: Calibrated minimum word confidence on the 0-to-1 scale.
    :return: Exact service content and one optional box per code point.
    :raises JobError: Invalid, partial, low-confidence, or unlocatable output.
    """
    try:
        text, pages = result["content"], result["pages"]
        width, height = size
        if not isinstance(text, str) or len(text) > MAX_CHARACTERS or len(pages) != 1:
            raise JobError("invalid_ocr_response")
        page = pages[0]
        if (
            page["pageNumber"] != 1
            or page["unit"] != "pixel"
            or page["width"] != width
            or page["height"] != height
        ):
            raise JobError("invalid_ocr_geometry")
        geometry = [None] * len(text)
        for word in page.get("words", []):
            confidence, polygon = word["confidence"], word["polygon"]
            start, length = word["span"]["offset"], word["span"]["length"]
            if (
                type(confidence) not in (int, float)
                or not math.isfinite(confidence)
                or not minimum <= confidence <= 1
            ):
                raise JobError("low_ocr_confidence")
            if (
                type(start) is not int
                or type(length) is not int
                or not 0 <= start < start + length <= len(text)
                or text[start : start + length] != word["content"]
            ):
                raise JobError("invalid_ocr_offsets")
            if len(polygon) != 8 or any(
                type(v) not in (int, float) or not math.isfinite(v) for v in polygon
            ):
                raise JobError("invalid_ocr_geometry")
            xs, ys = polygon[::2], polygon[1::2]
            box = (min(xs), min(ys), max(xs), max(ys))
            if not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
                raise JobError("invalid_ocr_geometry")
            for index in range(start, start + length):
                if geometry[index] is not None:
                    raise JobError("overlapping_ocr_spans")
                geometry[index] = box
        if any(not c.isspace() and box is None for c, box in zip(text, geometry)):
            raise JobError("unlocated_ocr_text")
        return text, geometry
    except (KeyError, TypeError, IndexError, ValueError):
        raise JobError("invalid_ocr_response") from None


class AzureRead:
    """Send image bytes privately; never ask OCR to fetch a blob or follow redirects.

    :param config: Validated worker configuration.
    :param credential: Explicit managed identity credential.
    :param session: Optional HTTP transport for deterministic tests.
    """

    def __init__(self, config, credential, session=None):
        import requests

        self.config = config
        self.credential = credential
        self.session = session or requests.Session()
        self.session.auth = lambda request: request

    def __call__(self, image):
        """OCR one raster with explicit deadlines, throttling, and complete coverage."""
        if image.width * image.height > MAX_PIXELS:
            raise JobError("image_pixel_limit")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        deadline = time.monotonic() + 180
        family = (
            "formrecognizer"
            if self.config.ocr_api_version == "2023-07-31"
            else "documentintelligence"
        )
        base = self.config.ocr_endpoint.rstrip("/")
        url = (
            f"{base}/{family}/documentModels/prebuilt-read:analyze"
            f"?api-version={self.config.ocr_api_version}&stringIndexType=unicodeCodePoint"
        )
        response = self._request("POST", url, deadline, data=stream.getvalue())
        if response.status_code != 202:
            raise JobError("ocr_submission_failed", response.status_code >= 500)
        operation = response.headers.get("Operation-Location", "")
        parsed, trusted = urlsplit(operation), urlsplit(base)
        if (
            parsed.scheme != "https"
            or parsed.netloc != trusted.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not parsed.path.startswith(f"/{family}/documentModels/prebuilt-read/")
        ):
            raise JobError("invalid_ocr_operation")
        while True:
            self._wait(response.headers.get("Retry-After", "1"), deadline)
            response = self._request("GET", operation, deadline)
            if response.status_code != 200:
                raise JobError("ocr_poll_failed", response.status_code >= 500)
            try:
                body = response.json()
                state = body["status"]
                if state == "succeeded":
                    normalized = normalize(
                        body["analyzeResult"], image.size, self.config.ocr_min_confidence
                    )
                    if not normalized[0].strip() and any(
                        low != high for low, high in image.convert("RGB").getextrema()
                    ):
                        raise JobError("empty_ocr_nonblank_image")
                    return normalized
                if state not in ("notStarted", "running"):
                    raise JobError("ocr_analysis_failed")
            except (ValueError, KeyError, TypeError):
                raise JobError("invalid_ocr_response") from None

    @staticmethod
    def _wait(value, deadline):
        try:
            delay = min(30, max(1, int(value)))
        except (ValueError, TypeError):
            delay = 1
        if time.monotonic() + delay >= deadline:
            raise JobError("ocr_deadline", True)
        time.sleep(delay)

    def _request(self, method, url, deadline, **kwargs):
        import requests
        from azure.core.exceptions import AzureError

        for attempt in range(4):
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                raise JobError("ocr_deadline", True)
            try:
                token = self.credential.get_token(self.config.ocr_scope).token
                response = self.session.request(
                    method,
                    url,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Content-Type": "application/octet-stream",
                    },
                    timeout=(min(10, remaining), min(30, remaining)),
                    allow_redirects=False,
                    verify=True,
                    **kwargs,
                )
            except AzureError:
                raise JobError("ocr_identity_failed") from None
            except requests.RequestException:
                raise JobError("ocr_transport_failed", True) from None
            if response.status_code not in (429, 502, 503, 504):
                return response
            if attempt < 3:
                self._wait(response.headers.get("Retry-After", "2"), deadline)
        raise JobError("ocr_throttled", True)
