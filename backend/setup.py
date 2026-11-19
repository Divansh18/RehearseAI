"""Package the frontend with the backend, without keeping a second copy in the source tree.

The browser pages, scripts and styles are maintained in ``frontend/`` at the repository root. The
server reads them from ``rehearse_ai/web`` when installed and from ``frontend/`` in a checkout (see
``rehearse_ai.server.frontend``). This file is the one place that connects the two:

* ``build_py`` copies ``frontend/{pages,scripts,styles}`` into ``rehearse_ai/web`` of the build
  output, so a wheel is self-contained. Nothing is written to the source tree.
* ``sdist`` adds the same files under ``frontend/`` in the archive, so a wheel built *from* the
  sdist (which has no sibling ``frontend/``) still contains them.

Everything else (prompts, fonts, licence) is ordinary package data declared in ``pyproject.toml``.
"""

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

HERE = Path(__file__).resolve().parent
PARTS = ("pages", "scripts", "styles")
SUFFIXES = {".html", ".js", ".css"}


def frontend_source() -> Path:
    """The checkout's ``frontend/`` or, inside an sdist, the copy the sdist carries."""
    for candidate in (HERE.parent / "frontend", HERE / "frontend"):
        if all((candidate / part).is_dir() for part in PARTS):
            return candidate
    raise SystemExit(
        "frontend/ (pages, scripts, styles) not found next to backend/ or in the sdist"
    )


def copy_frontend(destination: Path) -> None:
    source = frontend_source()
    for part in PARTS:
        for path in sorted((source / part).rglob("*")):
            if path.is_file() and path.suffix in SUFFIXES:
                target = destination / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)


class BuildWithFrontend(build_py):
    def run(self) -> None:
        super().run()
        copy_frontend(Path(self.build_lib) / "rehearse_ai" / "web")


class SdistWithFrontend(sdist):
    def make_release_tree(self, base_dir: str, files) -> None:
        super().make_release_tree(base_dir, files)
        copy_frontend(Path(base_dir) / "frontend")


setup(cmdclass={"build_py": BuildWithFrontend, "sdist": SdistWithFrontend})
