from __future__ import annotations

import json
import sys

from app.pdf_files import extract_pdf_text

WORKER_MEMORY_BYTES = 256 * 1024 * 1024
WORKER_CPU_SECONDS = 20


def _set_soft_limit(resource, limit_name: str, value: int) -> bool:
    """Lower one rlimit as far as this platform allows. Returns True when applied."""
    limit = getattr(resource, limit_name, None)
    if limit is None:
        return False
    try:
        _, hard = resource.getrlimit(limit)
    except (OSError, ValueError):
        return False
    if hard != resource.RLIM_INFINITY:
        value = min(value, hard)
    try:
        resource.setrlimit(limit, (value, hard))
    except (OSError, ValueError):
        return False
    return True


def _apply_resource_limits() -> None:
    """Best-effort sandbox for the extraction worker.

    Every limit is applied independently because platforms disagree about which
    ones exist. macOS rejects RLIMIT_AS outright even though it reports the hard
    limit as unlimited, so a failure here must not abort the worker; the caller
    still bounds it with PDF_EXTRACTION_TIMEOUT_SECONDS, and RLIMIT_CPU applies.
    """
    try:
        import resource
    except ImportError:
        return
    _set_soft_limit(resource, "RLIMIT_AS", WORKER_MEMORY_BYTES)
    _set_soft_limit(resource, "RLIMIT_DATA", WORKER_MEMORY_BYTES)
    _set_soft_limit(resource, "RLIMIT_CPU", WORKER_CPU_SECONDS)


def main() -> int:
    _apply_resource_limits()
    try:
        extraction = extract_pdf_text(sys.stdin.buffer.read())
    except ValueError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    sys.stdout.write(
        json.dumps(
            {
                "ok": True,
                "content": extraction.content,
                "page_count": extraction.page_count,
                "extraction_status": extraction.extraction_status,
                "extracted_text_bytes": extraction.extracted_text_bytes,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
