"""Where the browser pages, scripts and styles are read from.

The source lives in ``frontend/`` at the repository root. An installed wheel carries a copy under
``rehearse_ai/web`` (put there by ``backend/setup.py``). Reading prefers that packaged copy, so an
installed server never depends on a checkout, and falls back to ``frontend/`` when run from one.
Both have the same layout: ``pages/``, ``scripts/<audience>/`` and ``styles/``.
"""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
PARTS = ("pages", "scripts", "styles")


def frontend_directory() -> Path:
    packaged = PACKAGE_DIR / "web"
    if packaged.is_dir():
        return packaged
    checkout = PACKAGE_DIR.parents[2] / "frontend"
    if checkout.is_dir():
        return checkout
    raise RuntimeError("Frontend files not found: neither rehearse_ai/web nor frontend/ exists")


FRONTEND_DIR = frontend_directory()
