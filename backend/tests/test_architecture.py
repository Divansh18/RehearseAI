"""Dependency direction between the capability packages, enforced from the source tree.

Each package may import only the packages listed for it. The interview rules (``interview``) are
provider- and framework-independent: they must not import Pipecat, FastAPI, SQLAlchemy or any
vendor SDK. Nothing but ``__main__`` imports ``server``. To allow a new dependency, change this
table and ARCHITECTURE.md together, deliberately.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "rehearse_ai"

# package -> the other rehearse_ai packages it may import
ALLOWED = {
    "database": set(),
    "config": set(),
    "prompts": set(),
    "jobs": {"database"},
    "accounts": {"config", "database"},
    "plans": {"jobs", "database", "prompts"},
    "invitations": {"database", "plans"},
    "records": {"database", "plans"},
    "scheduling": {"accounts", "invitations", "jobs", "database", "plans"},
    "interview": {"plans"},
    "voice": {"config", "interview", "prompts"},
    "server": {
        "accounts",
        "config",
        "invitations",
        "jobs",
        "database",
        "plans",
        "records",
        "scheduling",
        "voice",
    },
    "__main__": {"server"},
}

# Vendor and framework imports the interview rules must never have.
INTERVIEW_FORBIDDEN = {
    "pipecat",
    "fastapi",
    "starlette",
    "sqlalchemy",
    "openai",
    "reportlab",
    "PIL",
    "deepgram",
    "cartesia",
}


def package_of(path: Path) -> str:
    relative = path.relative_to(SRC)
    return relative.parts[0] if len(relative.parts) > 1 else relative.stem


def imports_of(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


SOURCES = sorted(path for path in SRC.rglob("*.py") if "__pycache__" not in path.parts)


def test_every_source_package_is_covered_by_the_table():
    packages = {package_of(path) for path in SOURCES} - {"__init__"}
    assert packages <= set(ALLOWED), sorted(packages - set(ALLOWED))


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(SRC)))
def test_imports_follow_the_dependency_direction(path):
    package = package_of(path)
    if package == "__init__":
        return
    for module in imports_of(path):
        parts = module.split(".")
        if parts[0] != "rehearse_ai" or len(parts) < 2:
            continue
        target = parts[1]
        if target == package:
            continue
        assert target in ALLOWED[package], (
            f"{path.relative_to(SRC)} imports rehearse_ai.{target}; "
            f"{package} may import only {sorted(ALLOWED[package])}"
        )


def test_interview_rules_import_no_framework_or_vendor_sdk():
    for path in (SRC / "interview").rglob("*.py"):
        for module in imports_of(path):
            assert module.split(".")[0] not in INTERVIEW_FORBIDDEN, (path.name, module)


def test_no_module_other_than_main_imports_server():
    for path in SOURCES:
        if package_of(path) in {"server", "__main__"}:
            continue
        assert not any(m.startswith("rehearse_ai.server") for m in imports_of(path)), path
