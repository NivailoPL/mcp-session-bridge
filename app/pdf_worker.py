from __future__ import annotations

import json
import sys

from app.pdf_files import extract_pdf_text

WORKER_MEMORY_BYTES = 256 * 1024 * 1024
WORKER_CPU_SECONDS = 20


def _apply_resource_limits() -> None:
    try:
        import resource
    except ImportError:
        return
    resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY_BYTES, WORKER_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (WORKER_CPU_SECONDS, WORKER_CPU_SECONDS))


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
