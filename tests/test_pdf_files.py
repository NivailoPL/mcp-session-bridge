from __future__ import annotations

import asyncio
import subprocess
import threading
from io import BytesIO

import pytest
from pypdf import PdfWriter

import app.pdf_files as pdf_files
from tests.pdf_samples import make_pdf


def test_isolated_pdf_extraction_roundtrip() -> None:
    extraction = pdf_files.extract_pdf_text_isolated(make_pdf("Isolated PDF text"))

    assert extraction.page_count == 1
    assert extraction.extraction_status == "ready"
    assert "Isolated PDF text" in extraction.content


def test_pdf_extraction_rejects_encrypted_documents() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("secret")
    output = BytesIO()
    writer.write(output)

    with pytest.raises(ValueError, match="Encrypted or password-protected"):
        pdf_files.extract_pdf_text_isolated(output.getvalue())


def test_pdf_extraction_enforces_page_and_text_limits(monkeypatch) -> None:
    writer = PdfWriter()
    for _ in range(pdf_files.MAX_PDF_PAGES + 1):
        writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)

    with pytest.raises(ValueError, match="pages or fewer"):
        pdf_files.extract_pdf_text(output.getvalue())

    monkeypatch.setattr(pdf_files, "MAX_PDF_TEXT_BYTES", 20)
    with pytest.raises(ValueError, match="Extracted PDF text"):
        pdf_files.extract_pdf_text(make_pdf("Text that exceeds the test limit"))


def test_isolated_pdf_extraction_reports_timeout(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pdf-worker", timeout=1)

    monkeypatch.setattr(pdf_files.subprocess, "run", timeout)

    with pytest.raises(ValueError, match="timed out"):
        pdf_files.extract_pdf_text_isolated(make_pdf())


def test_pdf_worker_admission_is_bounded() -> None:
    release = threading.Event()

    def blocking_worker() -> str:
        release.wait(timeout=5)
        return "done"

    async def scenario() -> None:
        tasks = [
            asyncio.create_task(pdf_files.run_pdf_worker(blocking_worker))
            for _ in range(pdf_files._PDF_WORKER_MAX_ADMITTED)
        ]
        for _ in range(100):
            if pdf_files._pdf_worker_admitted == pdf_files._PDF_WORKER_MAX_ADMITTED:
                break
            await asyncio.sleep(0.01)
        with pytest.raises(pdf_files.PdfWorkerBusyError, match="busy"):
            await pdf_files.run_pdf_worker(lambda: "overflow")
        release.set()
        assert await asyncio.gather(*tasks) == ["done"] * len(tasks)

    asyncio.run(scenario())
    assert pdf_files._pdf_worker_admitted == 0


def test_cancelled_pdf_worker_keeps_its_slot_until_thread_finishes() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_worker() -> None:
        started.set()
        release.wait(timeout=5)

    async def scenario() -> None:
        task = asyncio.create_task(pdf_files.run_pdf_worker(blocking_worker))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pdf_files._pdf_worker_admitted == 1
        release.set()
        for _ in range(100):
            if pdf_files._pdf_worker_admitted == 0:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert pdf_files._pdf_worker_admitted == 0
