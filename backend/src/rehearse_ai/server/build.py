"""Fingerprints of the running source and prompts, recorded with every attempt."""

import hashlib
from importlib.metadata import version
from pathlib import Path

from rehearse_ai.server.frontend import FRONTEND_DIR, PACKAGE_DIR, PARTS

SUFFIXES = {".py", ".txt", ".html", ".css", ".js"}


def _fingerprinted_files() -> dict[str, Path]:
    """Python and prompt files of the package, plus the frontend files, under stable names.

    The frontend is named ``web/...`` whether it is read from a checkout or from a wheel, so the
    fingerprint of the same files is the same in both.
    """
    files = {
        str(path.relative_to(PACKAGE_DIR)): path
        for path in PACKAGE_DIR.rglob("*")
        if path.suffix in SUFFIXES and "web" not in path.relative_to(PACKAGE_DIR).parts[:1]
    }
    for part in PARTS:
        for path in (FRONTEND_DIR / part).rglob("*"):
            if path.suffix in SUFFIXES:
                files[f"web/{path.relative_to(FRONTEND_DIR)}"] = path
    return files


def source_build_id() -> str:
    """Freeze a source fingerprint when the server starts, even before a commit."""
    digest = hashlib.sha256()
    files = _fingerprinted_files()
    for name in sorted(files):
        digest.update(name.encode())
        digest.update(files[name].read_bytes())
    return f"{version('rehearse-ai')}+{digest.hexdigest()[:12]}"


def prompt_version() -> str:
    digest = hashlib.sha256()
    for prompt in sorted((PACKAGE_DIR / "prompts").glob("*.txt")):
        digest.update(prompt.name.encode())
        digest.update(prompt.read_bytes())
    return digest.hexdigest()[:16]
