from __future__ import annotations

import asyncio
import base64
import binascii
import json
import subprocess
import sys
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Callable, TypeVar

from pypdf import PdfReader
from pypdf import filters as pypdf_filters
from pypdf.errors import PyPdfError

MAX_ADMIN_PDF_BYTES = 20_000_000
MAX_MCP_PDF_BYTES = 10_000_000
MAX_PDF_PAGES = 500
MAX_PDF_TEXT_BYTES = 5_000_000
MAX_PDF_DECOMPRESSED_STREAM_BYTES = 10_000_000
PDF_EXTRACTION_TIMEOUT_SECONDS = 25
_PDF_WORKER_LIMIT = asyncio.Semaphore(2)
_PDF_WORKER_MAX_ADMITTED = 4
_pdf_worker_admitted = 0
T = TypeVar("T")

pypdf_filters.ZLIB_MAX_OUTPUT_LENGTH = MAX_PDF_DECOMPRESSED_STREAM_BYTES


@dataclass(frozen=True)
class PdfExtraction:
    content: str
    page_count: int
    extraction_status: str
    extracted_text_bytes: int


class PdfWorkerBusyError(RuntimeError):
    """Raised when the bounded PDF worker queue has no free admission slot."""


def _release_pdf_worker_slot(task: asyncio.Task[Any]) -> None:
    global _pdf_worker_admitted
    _pdf_worker_admitted -= 1
    if not task.cancelled():
        task.exception()


async def run_pdf_worker(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    global _pdf_worker_admitted
    if _pdf_worker_admitted >= _PDF_WORKER_MAX_ADMITTED:
        raise PdfWorkerBusyError("PDF processing is busy; retry shortly")
    _pdf_worker_admitted += 1

    async def execute() -> T:
        async with _PDF_WORKER_LIMIT:
            return await asyncio.to_thread(function, *args, **kwargs)

    task = asyncio.create_task(execute())
    task.add_done_callback(_release_pdf_worker_slot)
    return await asyncio.shield(task)


def extract_pdf_text_isolated(raw: bytes) -> PdfExtraction:
    try:
        process = subprocess.run(
            [sys.executable, "-m", "app.pdf_worker"],
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=PDF_EXTRACTION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("PDF text extraction timed out") from exc
    if process.returncode != 0:
        try:
            error_payload = json.loads(process.stdout)
            message = str(error_payload.get("error", "")).strip()
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            message = ""
        raise ValueError(message or "PDF text extraction exceeded safety limits")
    try:
        payload = json.loads(process.stdout)
        return PdfExtraction(
            content=str(payload["content"]),
            page_count=int(payload["page_count"]),
            extraction_status=str(payload["extraction_status"]),
            extracted_text_bytes=int(payload["extracted_text_bytes"]),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("PDF text extraction returned an invalid result") from exc


def decode_pdf_base64(value: str, *, max_bytes: int) -> bytes:
    if not isinstance(value, str):
        raise ValueError("content_base64 must be a string")
    max_encoded_bytes = ((max_bytes + 2) // 3) * 4
    if len(value) > max_encoded_bytes:
        raise ValueError(f"PDF must be {max_bytes} bytes or fewer")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("content_base64 must be valid base64") from exc
    if not raw:
        raise ValueError("PDF must not be empty")
    if len(raw) > max_bytes:
        raise ValueError(f"PDF must be {max_bytes} bytes or fewer")
    return raw


def extract_pdf_text(raw: bytes) -> PdfExtraction:
    if not raw.lstrip().startswith(b"%PDF-"):
        raise ValueError("File must be a valid PDF")
    try:
        reader = PdfReader(BytesIO(raw), strict=False)
        if reader.is_encrypted:
            raise ValueError("Encrypted or password-protected PDFs are not supported")
        page_count = len(reader.pages)
        if page_count == 0:
            raise ValueError("PDF must contain at least one page")
        if page_count > MAX_PDF_PAGES:
            raise ValueError(f"PDF must contain {MAX_PDF_PAGES} pages or fewer")

        sections: list[str] = []
        extracted_text_bytes = 0
        for page_number, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if not text:
                continue
            section = f"--- Page {page_number} ---\n{text}"
            section_bytes = len(section.encode("utf-8"))
            separator_bytes = 2 if sections else 0
            if extracted_text_bytes + separator_bytes + section_bytes > MAX_PDF_TEXT_BYTES:
                raise ValueError(
                    f"Extracted PDF text must be {MAX_PDF_TEXT_BYTES} bytes or fewer"
                )
            sections.append(section)
            extracted_text_bytes += separator_bytes + section_bytes
    except ValueError:
        raise
    except (PyPdfError, OSError, TypeError, KeyError, IndexError) as exc:
        raise ValueError("File must be a valid, readable PDF") from exc

    content = "\n\n".join(sections)
    return PdfExtraction(
        content=content,
        page_count=page_count,
        extraction_status="ready" if content else "no_text",
        extracted_text_bytes=extracted_text_bytes,
    )
