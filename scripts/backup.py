"""Create a confidential, consistent SQLite backup without exposing partial archives."""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile

from archive_utils import encrypted_api_keys, validate_encryption_key


def _check_integrity(connection):
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        detail = result[0] if result else "no integrity result"
        raise ValueError(f"SQLite integrity check failed: {detail}")


def create_backup(data, output):
    database = data / "lexcite.sqlite3"
    key_path = data / "secret.key"
    if not database.is_file():
        raise ValueError(f"No database found at {database}")
    if output.exists():
        raise ValueError(f"Refusing to overwrite an existing backup: {output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    temp_archive = None
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "lexcite.sqlite3"
            with contextlib.closing(sqlite3.connect(database)) as source:
                _check_integrity(source)
                encrypted_values = encrypted_api_keys(source)
                key = key_path.read_bytes() if key_path.is_file() else None
                validate_encryption_key(key, encrypted_values)
                with contextlib.closing(sqlite3.connect(target)) as dest:
                    source.backup(dest)
                    tables = {
                        row[0]
                        for row in dest.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    if "sessions" in tables:
                        dest.execute("DELETE FROM sessions")
                    dest.commit()
                    _check_integrity(dest)

            fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".partial", dir=output.parent)
            temp_archive = Path(temp_name)
            with os.fdopen(fd, "wb") as stream:
                os.chmod(temp_archive, 0o600)
                with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.write(target, "lexcite.sqlite3")
                    if key_path.is_file():
                        archive.write(key_path, "secret.key")
                    archive.writestr(
                        "backup.json",
                        json.dumps(
                            {
                                "version": "1.4.0",
                                "created": dt.datetime.now(dt.timezone.utc).isoformat(),
                            }
                        ),
                    )
                stream.flush()
                os.fsync(stream.fileno())

        # A hard link publishes the complete archive atomically and fails if
        # another process created the requested destination in the meantime.
        os.link(temp_archive, output)
    finally:
        if temp_archive is not None:
            try:
                temp_archive.unlink()
            except FileNotFoundError:
                pass

    return output.resolve()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=os.getenv("LEXCITE_DATA", "data"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        result = create_backup(Path(args.data), Path(args.output))
    except (OSError, sqlite3.Error, ValueError, zipfile.BadZipFile) as exc:
        raise SystemExit(f"Backup failed: {exc}") from exc
    print(result)


if __name__ == "__main__":
    main()
