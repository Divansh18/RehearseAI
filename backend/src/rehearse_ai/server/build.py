"""Fingerprints of the running source and prompts, recorded with every attempt."""

import hashlib
from importlib.metadata import version
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]


def source_build_id() -> str:
    """Freeze a source fingerprint when the server starts, even before a commit."""
    digest = hashlib.sha256()
    for path in sorted(PACKAGE_DIR.rglob("*")):
        if path.suffix in {".py", ".txt", ".html", ".css", ".js"}:
            digest.update(str(path.relative_to(PACKAGE_DIR)).encode())
            digest.update(path.read_bytes())
    return f"{version('rehearse-ai')}+{digest.hexdigest()[:12]}"


def prompt_version() -> str:
    digest = hashlib.sha256()
    for prompt in sorted((PACKAGE_DIR / "prompts").glob("*.txt")):
        digest.update(prompt.name.encode())
        digest.update(prompt.read_bytes())
    return digest.hexdigest()[:16]
