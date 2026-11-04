"""Strict non-secret configuration and explicit identity selection."""

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .contract import JobError, opaque


def endpoint(value: str) -> str:
    """Require a root HTTPS endpoint without credentials, paths, or parameters."""
    if not isinstance(value, str):
        raise JobError("invalid_endpoint")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.port not in (443, None)
        or parsed.query
        or parsed.fragment
    ):
        raise JobError("invalid_endpoint")
    return value.rstrip("/")


@dataclass(frozen=True)
class Config:
    """Configuration loaded only by trusted deployment operators.

    :param identity: Explicit ``user`` or ``system`` managed identity mode.
    :param client_id: UAMI client ID, empty only for deliberate SAMI mode.
    """

    identity: str
    client_id: str
    blob_endpoint: str
    queue_endpoint: str
    input_container: str
    output_container: str
    queue_name: str
    sql_server: str
    sql_database: str
    analyzer_endpoint: str
    analyzer_resource: str
    ocr_endpoint: str
    ocr_scope: str
    ocr_api_version: str
    ocr_min_confidence: float
    ocr_validated: bool = False
    processing_enabled: bool = False
    lease_seconds: int = 180
    job_timeout_seconds: int = 1800
    max_attempts: int = 3
    poll_seconds: int = 5

    def __post_init__(self):
        for field in (
            "identity",
            "client_id",
            "input_container",
            "output_container",
            "queue_name",
            "sql_server",
            "sql_database",
            "analyzer_resource",
            "ocr_scope",
            "ocr_api_version",
        ):
            if not isinstance(getattr(self, field), str):
                raise JobError("invalid_configuration")
        if self.identity not in ("user", "system"):
            raise JobError("invalid_identity")
        if self.identity == "user":
            opaque(self.client_id)
        elif self.client_id:
            raise JobError("invalid_identity")
        for field in ("blob_endpoint", "queue_endpoint", "analyzer_endpoint", "ocr_endpoint"):
            endpoint(getattr(self, field))
        if not self.blob_endpoint.endswith(".blob.core.usgovcloudapi.net"):
            raise JobError("invalid_government_storage")
        if not self.queue_endpoint.endswith(".queue.core.usgovcloudapi.net"):
            raise JobError("invalid_government_storage")
        if not self.ocr_endpoint.endswith(".cognitiveservices.azure.us"):
            raise JobError("invalid_government_ocr_endpoint")
        if not re.fullmatch(r"[a-z0-9-]+\.database\.usgovcloudapi\.net", self.sql_server):
            raise JobError("invalid_sql_server")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.sql_database):
            raise JobError("invalid_sql_database")
        for name in (self.input_container, self.output_container, self.queue_name):
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{1,61})[a-z0-9]", name):
                raise JobError("invalid_storage_name")
        if self.input_container == self.output_container:
            raise JobError("separate_input_output_containers")
        if (
            not self.analyzer_resource
            or self.analyzer_resource.endswith("/.default")
            or any(c in self.analyzer_resource for c in "?# \r\n")
            or not self.analyzer_resource.startswith(("api://", "https://"))
        ):
            raise JobError("invalid_api_resource")
        if self.ocr_scope != "https://cognitiveservices.azure.us/.default":
            raise JobError("invalid_government_ocr_scope")
        if self.ocr_api_version not in ("2023-07-31", "2024-11-30"):
            raise JobError("unsupported_ocr_api")
        if type(self.ocr_min_confidence) not in (int, float) or not (
            0 <= self.ocr_min_confidence <= 1
        ):
            raise JobError("invalid_ocr_confidence")
        if type(self.processing_enabled) is not bool or type(self.ocr_validated) is not bool:
            raise JobError("invalid_activation_gate")
        if self.processing_enabled and not self.ocr_validated:
            raise JobError("ocr_acceptance_required")
        for number, low, high in (
            (self.lease_seconds, 60, 600),
            (self.job_timeout_seconds, 60, 7200),
            (self.max_attempts, 1, 10),
            (self.poll_seconds, 1, 60),
        ):
            if type(number) is not int or not low <= number <= high:
                raise JobError("invalid_operational_limit")

    @classmethod
    def load(cls, path):
        """Load a bounded JSON file; unknown/credential fields fail explicitly."""
        try:
            if path.stat().st_size > 16_384:
                raise JobError("invalid_configuration")
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except (TypeError, ValueError, OSError):
            raise JobError("invalid_configuration") from None

    def credential(self):
        """Return only the configured identity, without credential-chain fallbacks."""
        from azure.identity import ManagedIdentityCredential

        return (
            ManagedIdentityCredential(client_id=self.client_id)
            if self.identity == "user"
            else ManagedIdentityCredential()
        )
