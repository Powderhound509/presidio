"""Versioned, opaque job references; no caller-selected URLs or filesystem paths."""

import re
from dataclasses import dataclass
from uuid import UUID

POLICY = "native-v1"
EXTENSIONS = {"pdf", "txt", "docx", "doc", "xlsx", "png", "jpg", "jpeg", "tif", "tiff"}
OPERATIONS = {"annotate", "redact", "sanitize"}
TERMINAL = {"succeeded", "succeeded_with_warnings", "failed", "dead_letter"}
MAX_BYTES = 100 * 1024 * 1024
MAX_CHARACTERS = 2_000_000
MAX_PIXELS = 25_000_000
MAX_TOTAL_PIXELS = 250_000_000
MAX_FRAMES = 200
MAX_CELLS = 100_000
MAX_SHEETS = 100
MAX_ZIP_ENTRIES = 1000
MAX_EXPANDED_BYTES = 100 * 1024 * 1024


class JobError(Exception):
    """Privacy-safe error code suitable for durable status.

    :param code: Fixed operational code, never source text.
    :param retryable: Whether the worker may retry this failure.
    """

    def __init__(self, code: str, retryable: bool = False):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("Invalid error code.")
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def opaque(value: str) -> str:
    """Validate and canonicalize a UUID without returning the rejected value."""
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise JobError("invalid_identifier") from None


@dataclass(frozen=True)
class Submission:
    """Immutable version reference, scoped by the authenticated SQL principal."""

    document_id: str
    version_id: str
    extension: str
    etag: str
    operation: str
    language: str = "en"
    policy: str = POLICY

    def __post_init__(self):
        object.__setattr__(self, "document_id", opaque(self.document_id))
        object.__setattr__(self, "version_id", opaque(self.version_id))
        if self.extension not in EXTENSIONS or self.operation not in OPERATIONS:
            raise JobError("unsupported_capability")
        if not re.fullmatch(r"[a-z]{2}", self.language) or self.policy != POLICY:
            raise JobError("unsupported_policy")
        if not re.fullmatch(r'(?:0x[0-9A-Fa-f]{1,32}|"0x[0-9A-Fa-f]{1,32}")', self.etag):
            raise JobError("invalid_etag")
        object.__setattr__(self, "etag", '"' + self.etag.strip('"') + '"')

    @property
    def blob(self) -> str:
        """Return the sole allowed input path shape."""
        return f"{self.document_id}/{self.version_id}.{self.extension}"


def queue_message(job_id: str) -> str:
    """Encode an opaque job ID with an explicit protocol version."""
    return "1:" + opaque(job_id)


def parse_message(body: str) -> str:
    """Reject arbitrary queue payloads rather than interpreting paths or JSON."""
    if not isinstance(body, str) or not body.startswith("1:") or len(body) != 38:
        raise JobError("invalid_queue_message")
    return opaque(body[2:])
