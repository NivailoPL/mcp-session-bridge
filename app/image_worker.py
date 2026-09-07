from __future__ import annotations

import json
import sys
import warnings
from io import BytesIO

from app.image_files import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS
from app.pdf_worker import _apply_resource_limits


def inspect_image(raw: bytes) -> str:
    from PIL import Image, UnidentifiedImageError

    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Image must not be empty and must be at most 10 MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw), formats=["JPEG", "PNG"]) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("Image must be at most 40 megapixels")
                if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Animated images are not supported")
                mime_type = "image/jpeg" if image.format == "JPEG" else "image/png"
                image.verify()
            # verify checks the container; load also checks the compressed pixels.
            with Image.open(BytesIO(raw), formats=["JPEG", "PNG"]) as image:
                image.load()
            return mime_type
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError("Invalid image or image exceeds safety limits") from exc


def main() -> int:
    _apply_resource_limits()
    try:
        mime_type = inspect_image(sys.stdin.buffer.read(MAX_IMAGE_BYTES + 1))
    except (ValueError, MemoryError) as exc:
        sys.stdout.write(json.dumps({"error": str(exc) or "Image exceeded memory limit"}))
        return 2
    sys.stdout.write(json.dumps({"mime_type": mime_type}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
