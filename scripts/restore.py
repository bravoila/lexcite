"""Offline restore: stop the service and install only into an empty data directory."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile

from archive_utils import encrypted_api_keys, validate_encryption_key


DATABASE_NAME = "lexcite.sqlite3"
KEY_NAME = "secret.key"


def _validate_database(path):
    try:
        with contextlib.closing(sqlite3.connect(path)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                detail = result[0] if result else "no integrity result"
                raise ValueError(f"SQLite integrity check failed: {detail}")
            encrypted_values = encrypted_api_keys(connection)
    except sqlite3.Error as exc:
        raise ValueError(f"invalid SQLite database: {exc}") from exc
    return encrypted_values


def _install_staged(stage, data, names):
    created_data_dir = False
    if data.is_symlink():
        raise ValueError(f"restore destination must not be a symbolic link: {data}")
    if not data.exists():
        data.mkdir(parents=True)
        created_data_dir = True
    elif not data.is_dir():
        raise ValueError(f"restore destination is not a directory: {data}")

    if any(data.iterdir()):
        raise ValueError(
            "restore destination is not empty; move or back up existing user data before restoring"
        )

    installed = []
    try:
        for name in names:
            target = data / name
            # Hard-linking is atomic and does not replace a file created by
            # another process after the empty-directory check.
            os.link(stage / name, target)
            installed.append(target)
            target.chmod(0o600)
        try:
            data.chmod(0o700)
        except OSError:
            pass
    except Exception:
        for target in installed:
            try:
                target.unlink()
            except OSError:
                pass
        if created_data_dir:
            try:
                data.rmdir()
            except OSError:
                pass
        raise


def restore(archive_path, data):
    if data.is_symlink():
        raise ValueError(f"restore destination must not be a symbolic link: {data}")
    if data.exists():
        if not data.is_dir():
            raise ValueError(f"restore destination is not a directory: {data}")
        if any(data.iterdir()):
            raise ValueError(
                "restore destination is not empty; move or back up existing user data before restoring"
            )

    if not archive_path.is_file():
        raise ValueError(f"backup archive not found: {archive_path}")

    data.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".lexcite-restore-", dir=data.parent) as stage_name:
        stage = Path(stage_name)
        try:
            with zipfile.ZipFile(archive_path, "r") as source:
                names = source.namelist()
                if len(names) != len(set(names)):
                    raise ValueError("backup archive contains duplicate member names")
                if DATABASE_NAME not in names:
                    raise ValueError(f"backup archive is missing required member {DATABASE_NAME}")
                if source.testzip() is not None:
                    raise ValueError("backup ZIP has a corrupt member or checksum")
                for name in (DATABASE_NAME, KEY_NAME):
                    if name in names:
                        (stage / name).write_bytes(source.read(name))
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise ValueError(f"cannot read backup archive: {exc}") from exc

        encrypted_values = _validate_database(stage / DATABASE_NAME)
        key_in_archive = (stage / KEY_NAME).is_file()
        key = (stage / KEY_NAME).read_bytes() if key_in_archive else None
        validate_encryption_key(key, encrypted_values)

        install_names = [DATABASE_NAME]
        if key_in_archive:
            install_names.append(KEY_NAME)
        _install_staged(stage, data, install_names)

    return data


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    parser.add_argument("--data", default="data")
    args = parser.parse_args(argv)
    try:
        restore(Path(args.archive), Path(args.data))
    except (OSError, sqlite3.Error, ValueError, zipfile.BadZipFile) as exc:
        raise SystemExit(f"Restore failed: {exc}") from exc
    print("Restore validated and installed. Start the service and log in again.")


if __name__ == "__main__":
    main()
