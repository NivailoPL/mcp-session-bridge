"""Bounded validation of original image attachments; no image transformation."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

MAX_IMAGE_BYTES = 10_000_000
MAX_IMAGE_PIXELS = 40_000_000
IMAGE_TIMEOUT_SECONDS = 25
IMAGE_EXTENSIONS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
_IMAGE_WORKER_LIMIT = asyncio.Semaphore(2)
_IMAGE_WORKER_MAX_ADMITTED = 4
_image_worker_admitted = 0
T = TypeVar("T")


class ImageWorkerBusyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    mime_type: str


def _release_slot(task: asyncio.Task[Any]) -> None:
    global _image_worker_admitted
    _image_worker_admitted -= 1
    if not task.cancelled():
        task.exception()


async def run_image_worker(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    global _image_worker_admitted
    if _image_worker_admitted >= _IMAGE_WORKER_MAX_ADMITTED:
        raise ImageWorkerBusyError("Image processing is busy; retry shortly")
    _image_worker_admitted += 1

    async def execute() -> T:
        async with _IMAGE_WORKER_LIMIT:
            return await asyncio.to_thread(function, *args, **kwargs)

    task = asyncio.create_task(execute())
    task.add_done_callback(_release_slot)
    return await asyncio.shield(task)


def decode_image_base64(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError("content_base64 must be a string")
    if len(value) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("Image must be at most 10 MB")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("content_base64 must be valid base64") from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Image must not be empty and must be at most 10 MB")
    return raw


def validate_image_isolated(filename: str, raw: bytes) -> ValidatedImage:
    expected = IMAGE_EXTENSIONS.get(Path(filename).suffix.lower())
    if expected is None:
        raise ValueError("Supported image extensions are .jpg, .jpeg and .png")
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Image must not be empty and must be at most 10 MB")
    try:
        process = subprocess.run(
            [sys.executable, "-m", "app.image_worker"], input=raw,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=IMAGE_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Image validation timed out") from exc
    try:
        payload = json.loads(process.stdout)
        if not isinstance(payload, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Image validation failed or exceeded safety limits") from exc
    if process.returncode != 0:
        raise ValueError(str(payload.get("error") or "Image validation failed"))
    if payload.get("mime_type") != expected:
        raise ValueError("Image format does not match its filename extension")
    return ValidatedImage(raw, expected)
