"""Relocate local application data without removing the original copy."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

LOCATION_NAME = "storage-location.json"


def configured_paths(default_data: Path, default_config: Path) -> tuple[Path, Path]:
    pointer = default_config / LOCATION_NAME
    if not pointer.exists():
        return default_data, default_config
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        root = Path(data["root"])
        if (
            not root.is_absolute()
            or not (root / "data" / "inbox.sqlite3").is_file()
            or not (root / "config" / "config.json").is_file()
        ):
            raise ValueError(
                "La ubicación, la base de datos o la configuración no están disponibles."
            )
        return root / "data", root / "config"
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(
            f"No se puede abrir la ubicación de BacklogTalk indicada en {pointer}: {error}"
        ) from error


def digest(path: Path) -> bytes:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.digest()


def copy_tree(source: Path, destination: Path, excluded: set[str]) -> None:
    if not source.exists():
        return
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if relative.parts[0] in excluded:
            continue
        if path.is_symlink():
            raise ValueError(
                f"La carpeta contiene un enlace simbólico que no se puede copiar automáticamente: {path}"
            )
        output = destination / relative
        if path.is_dir():
            output.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
            if digest(path) != digest(output):
                raise OSError(f"La copia no coincide con el archivo original: {path}")


def relocate(data: Path, config: Path, pointer: Path, parent: Path) -> Path:
    """Copy into parent/BacklogTalk-data, then atomically switch the location pointer.

    Caller must stop all database, audio and background activity for the duration.
    Existing sources are deliberately retained. Destination must not exist.
    """
    data = data.resolve()
    config = config.resolve()
    parent = parent.resolve()
    if not parent.is_dir():
        raise ValueError("Elegí una carpeta existente.")
    final = parent / "BacklogTalk-data"
    if final.exists() or final.is_symlink():
        raise ValueError(f"Ya existe {final}. Elegí otra carpeta para no sobrescribir datos.")
    if any(
        final == source or final.is_relative_to(source) or source.is_relative_to(final)
        for source in (data, config)
    ):
        raise ValueError(
            "Elegí una carpeta que no esté dentro de los datos actuales ni los contenga."
        )
    if not (data / "inbox.sqlite3").is_file():
        raise FileNotFoundError("No se encontró la base de datos actual.")
    staging = Path(tempfile.mkdtemp(prefix=".backlogtalk-copy-", dir=parent))
    final_created = False
    pointer_switched = False
    try:
        new_data = staging / "data"
        new_config = staging / "config"
        new_data.mkdir()
        new_config.mkdir()
        excluded = {"inbox.sqlite3", "inbox.sqlite3-wal", "inbox.sqlite3-shm"}
        if data == config:
            excluded |= {"config.json", "SKILL.md", LOCATION_NAME}
        copy_tree(data, new_data, excluded)
        if config == data:
            for name in ("config.json", "SKILL.md"):
                src = config / name
                if src.is_symlink():
                    raise ValueError(f"No se puede copiar un enlace simbólico: {src}")
                if src.is_file():
                    shutil.copy2(src, new_config / name)
                    if digest(src) != digest(new_config / name):
                        raise OSError(f"No coincide la copia: {src}")
        else:
            copy_tree(config, new_config, {LOCATION_NAME})
        if not (new_config / "config.json").is_file():
            raise FileNotFoundError("No se encontró la configuración actual para copiar.")
        source = sqlite3.connect(data / "inbox.sqlite3")
        target = sqlite3.connect(new_data / "inbox.sqlite3")
        try:
            source.backup(target)
            for capture_id, audio in target.execute(
                "SELECT id, audio_file FROM captures WHERE audio_file IS NOT NULL"
            ).fetchall():
                path = Path(audio)
                try:
                    relative = path.resolve().relative_to((data / "audio").resolve())
                except (ValueError, OSError):
                    continue
                if not (new_data / "audio" / relative).is_file():
                    raise FileNotFoundError(
                        f"Falta el audio guardado de la nota #{capture_id}: {path}"
                    )
                target.execute(
                    "UPDATE captures SET audio_file=? WHERE id=?",
                    (str(final / "data" / "audio" / relative), capture_id),
                )
            if target.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='discarded_mix_jobs'"
            ).fetchone():
                for (manifest,) in target.execute(
                    "SELECT manifest FROM discarded_mix_jobs"
                ).fetchall():
                    try:
                        relative = Path(manifest).resolve().relative_to((data / "audio").resolve())
                    except (ValueError, OSError):
                        continue
                    target.execute(
                        "UPDATE discarded_mix_jobs SET manifest=? WHERE manifest=?",
                        (str(final / "data" / "audio" / relative), manifest),
                    )
            target.commit()
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("La verificación de la base de datos copiada falló.")
        finally:
            target.close()
            source.close()
        os.rename(staging, final)
        final_created = True
        pointer.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".backlogtalk-location-", dir=pointer.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump({"root": str(final)}, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, pointer)
            pointer_switched = True
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return final
    except BaseException:
        if final_created and not pointer_switched:
            shutil.rmtree(final, ignore_errors=True)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
