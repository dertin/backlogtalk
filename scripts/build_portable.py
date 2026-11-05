#!/usr/bin/env python3
"""Build a source distribution without local data, credentials or virtualenvs."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "dist" / "backlogtalk-portable.zip"
FILES = [
    "README.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "LICENSE",
    "backlogtalk/__init__.py",
    "backlogtalk/version.py",
    "backlogtalk/storage.py",
    "backlogtalk/shortcuts.py",
    "backlogtalk/qt_app.py",
    "backlogtalk/qt_widgets.py",
    "backlogtalk/audio_io.py",
    "backlogtalk/core.py",
    "backlogtalk/epic_manager.py",
    "backlogtalk/instance.py",
    "backlogtalk/media_safety.py",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "docs/guia.md",
    "docs/images/backlogtalk-banner.svg",
    "docs/images/backlogtalk-captura.png",
    "assets/chevron.svg",
    "assets/backlogtalk-mark.svg",
    "scripts/install.sh",
    "scripts/bootstrap_pip.py",
    "scripts/run.sh",
    "scripts/install.ps1",
    "scripts/run.ps1",
    "resources/default-ticket-instructions.md",
    ".github/workflows/compatibility.yml",
    "scripts/build_portable.py",
]


def main():
    paths = [ROOT / name for name in FILES] + sorted((ROOT / "tests").glob("test_*.py"))
    for path in paths:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError(f"Archivo no disponible o fuera del proyecto: {path}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(OUTPUT, "w", compression=ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, Path("backlogtalk") / path.relative_to(ROOT))
    print(f"Paquete creado: {OUTPUT}")


if __name__ == "__main__":
    main()
