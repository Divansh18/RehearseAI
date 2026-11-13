"""Validate and privately store one browser-submitted camera still."""

import io
import os
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

MAX_UPLOAD_BYTES = 1_000_000
MAX_INPUT_SIZE = (1920, 1080)
MAX_OUTPUT_SIZE = (640, 360)


class InvalidSnapshot(ValueError):
    pass


def save_snapshot(raw: bytes, target: Path) -> None:
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise InvalidSnapshot("Camera image must be under 1 MB")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "JPEG" or not all(
                16 <= side <= limit for side, limit in zip(image.size, MAX_INPUT_SIZE, strict=True)
            ):
                raise InvalidSnapshot("Invalid camera image format or dimensions")
            image.load()
            clean = image.convert("RGB")
            clean.thumbnail(MAX_OUTPUT_SIZE, Image.Resampling.LANCZOS)
            output = io.BytesIO()
            clean.save(output, format="JPEG", quality=78, optimize=True)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidSnapshot("Invalid camera image") from exc
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    temp = target.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(output.getvalue())
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
