"""Validate and privately store one browser-submitted image for the Conversation PDF.

The image is evidence of what the browser reported at one moment. It is not identity verification
and it is not tamper-proof: the browser is the candidate's, and anything it sends can be
fabricated. Nothing about the file (its name, its claims about itself, any metadata) is trusted;
the bytes are decoded, checked against fixed limits, and re-encoded.

Two kinds are accepted:
- ``room``: a browser-rendered picture of the interview room (camera, panel, status, controls).
- ``camera``: the labelled fallback, a camera-only still.
"""

import io
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

KINDS = ("room", "camera")
# The largest body any kind may send; the per-kind limit below is applied after reading.
MAX_UPLOAD_BYTES = 1_500_000


@dataclass(frozen=True)
class Limits:
    max_bytes: int
    min_side: int
    max_width: int
    max_height: int
    max_pixels: int
    output: tuple[int, int]


LIMITS = {
    "camera": Limits(1_000_000, 16, 1920, 1080, 1920 * 1080, (640, 360)),
    "room": Limits(1_500_000, 240, 1920, 2400, 3_500_000, (1280, 2000)),
}


class InvalidSnapshot(ValueError):
    pass


def save_snapshot(raw: bytes, target: Path, kind: str = "camera") -> tuple[int, int]:
    """Decode, validate, re-encode and store the image; returns the stored width and height."""
    if kind not in LIMITS:
        raise InvalidSnapshot("Unknown image kind")
    limits = LIMITS[kind]
    if not raw or len(raw) > limits.max_bytes:
        raise InvalidSnapshot(f"Image must be under {limits.max_bytes // 1_000_000 or 1} MB")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            if (
                image.format != "JPEG"
                or min(width, height) < limits.min_side
                or width > limits.max_width
                or height > limits.max_height
                or width * height > limits.max_pixels
            ):
                raise InvalidSnapshot("Invalid image format or dimensions")
            image.load()
            clean = image.convert("RGB")
            clean.thumbnail(limits.output, Image.Resampling.LANCZOS)
            output = io.BytesIO()
            clean.save(output, format="JPEG", quality=82, optimize=True)
            stored = clean.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidSnapshot("Invalid image") from exc
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    temp = target.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(output.getvalue())
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return stored
