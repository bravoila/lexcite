import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

from cryptography.fernet import Fernet
import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "scripts" / "backup.py"
RESTORE = ROOT / "scripts" / "restore.py"


def _run(script, *args):
    return subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _database(path, *, encrypted=False):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("CREATE TABLE sessions (token TEXT PRIMARY KEY, expires REAL NOT NULL)")
        connection.execute("INSERT INTO sessions(token, expires) VALUES ('fixture-session', 1)")
        if encrypted:
            key = Fernet.generate_key()
            token = Fernet(key).encrypt(b"fixture-secret").decode()
            connection.execute(
                "INSERT INTO kv(key, value) VALUES (?, ?)",
                ("settings", json.dumps({"api_key_encrypted": token})),
            )
            return key


def _archive(path, *, database=True, key=None, corrupt_database=False):
    with zipfile.ZipFile(path, "w") as archive:
        if database:
            if corrupt_database:
                archive.writestr("lexcite.sqlite3", b"not a SQLite database")
            else:
                db_path = path.parent / f"{path.stem}-fixture.sqlite3"
                _database(db_path)
                archive.write(db_path, "lexcite.sqlite3")
        if key is not None:
            archive.writestr("secret.key", key)
        archive.writestr("backup.json", '{"version":"1.4.0"}')


def test_restore_rejects_empty_or_database_less_archive_without_installing(tmp_path):
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w"):
        pass
    missing_db = tmp_path / "missing-db.zip"
    _archive(missing_db, database=False)

    for archive in (empty, missing_db):
        destination = tmp_path / f"restore-{archive.stem}"
        result = _run(RESTORE, archive, "--data", destination)
        assert result.returncode != 0
        assert not (destination / "lexcite.sqlite3").exists()
        assert "restored" not in result.stdout.lower()


def test_restore_requires_key_when_database_contains_encrypted_settings(tmp_path):
    db_path = tmp_path / "encrypted.sqlite3"
    _database(db_path, encrypted=True)
    archive_path = tmp_path / "without-key.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(db_path, "lexcite.sqlite3")
        archive.writestr("backup.json", '{"version":"1.4.0"}')
    destination = tmp_path / "restore"

    result = _run(RESTORE, archive_path, "--data", destination)

    assert result.returncode != 0
    assert not destination.exists() or not any(destination.iterdir())
    assert "key" in (result.stderr + result.stdout).lower()


@pytest.mark.parametrize(
    "setting_name,settings",
    [
        ("settings", {"api_key_encrypted": "token"}),
        ("search_settings", {"api_key_encrypted": "token"}),
        ("retrieval_settings", {"embedding": {"api_key_encrypted": "token"}}),
    ],
)
def test_restore_requires_key_for_each_encrypted_service(tmp_path, setting_name, settings):
    db_path = tmp_path / f"{setting_name}.sqlite3"
    _database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?)",
            (setting_name, json.dumps(settings)),
        )
    archive_path = tmp_path / f"{setting_name}.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(db_path, "lexcite.sqlite3")
        archive.writestr("backup.json", '{"version":"1.4.0"}')

    result = _run(RESTORE, archive_path, "--data", tmp_path / f"restore-{setting_name}")

    assert result.returncode != 0
    assert "secret.key" in (result.stderr + result.stdout)


def test_restore_rejects_valid_key_that_cannot_decrypt_settings(tmp_path):
    db_path = tmp_path / "encrypted.sqlite3"
    _database(db_path)
    correct_key = Fernet.generate_key()
    token = Fernet(correct_key).encrypt(b"fixture-secret").decode()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?)",
            ("settings", json.dumps({"api_key_encrypted": token})),
        )
    archive_path = tmp_path / "wrong-key.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(db_path, "lexcite.sqlite3")
        archive.writestr("secret.key", Fernet.generate_key())
        archive.writestr("backup.json", '{"version":"1.4.0"}')

    result = _run(RESTORE, archive_path, "--data", tmp_path / "restore-wrong-key")

    assert result.returncode != 0
    assert "cannot decrypt" in (result.stderr + result.stdout)


def test_restore_rejects_corrupt_database_and_corrupt_zip_without_installing(tmp_path):
    corrupt_db = tmp_path / "corrupt-db.zip"
    _archive(corrupt_db, corrupt_database=True)
    corrupt_zip = tmp_path / "corrupt.zip"
    corrupt_zip.write_bytes(b"not a ZIP archive")

    for archive in (corrupt_db, corrupt_zip):
        destination = tmp_path / f"restore-{archive.stem}"
        result = _run(RESTORE, archive, "--data", destination)
        assert result.returncode != 0
        assert not (destination / "lexcite.sqlite3").exists()
        assert "restored" not in result.stdout.lower()


def test_restore_does_not_overwrite_existing_user_data(tmp_path):
    archive_path = tmp_path / "valid.zip"
    _archive(archive_path)
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "notes.txt"
    sentinel.write_text("keep this data")

    result = _run(RESTORE, archive_path, "--data", destination)

    assert result.returncode != 0
    assert sentinel.read_text() == "keep this data"
    assert not (destination / "lexcite.sqlite3").exists()


def test_legacy_backup_without_key_is_valid_when_settings_are_unencrypted(tmp_path):
    archive_path = tmp_path / "legacy-no-key.zip"
    _archive(archive_path)
    destination = tmp_path / "restore"

    result = _run(RESTORE, archive_path, "--data", destination)

    assert result.returncode == 0, result.stderr
    assert (destination / "lexcite.sqlite3").is_file()
    assert not (destination / "secret.key").exists()
    assert "validated and installed" in result.stdout.lower()


def test_backup_and_restore_preserve_a_matching_encryption_key(tmp_path):
    data = tmp_path / "encrypted-data"
    data.mkdir()
    key = _database(data / "lexcite.sqlite3", encrypted=True)
    (data / "secret.key").write_bytes(key)
    backup = tmp_path / "encrypted-backup.zip"
    restored = tmp_path / "restored"

    created = _run(BACKUP, "--data", data, "--output", backup)
    assert created.returncode == 0, created.stderr
    installed = _run(RESTORE, backup, "--data", restored)

    assert installed.returncode == 0, installed.stderr
    assert (restored / "secret.key").read_bytes() == key
    with sqlite3.connect(restored / "lexcite.sqlite3") as connection:
        token = json.loads(connection.execute(
            "SELECT value FROM kv WHERE key='settings'"
        ).fetchone()[0])["api_key_encrypted"]
    assert Fernet(key).decrypt(token.encode()) == b"fixture-secret"


def test_backup_missing_database_and_corrupt_database_do_not_create_final_archive(tmp_path):
    missing_data = tmp_path / "missing"
    missing_data.mkdir()
    missing_output = tmp_path / "missing.zip"
    result = _run(BACKUP, "--data", missing_data, "--output", missing_output)
    assert result.returncode != 0
    assert not missing_output.exists()

    corrupt_data = tmp_path / "corrupt"
    corrupt_data.mkdir()
    (corrupt_data / "lexcite.sqlite3").write_bytes(b"not a SQLite database")
    corrupt_output = tmp_path / "corrupt.zip"
    result = _run(BACKUP, "--data", corrupt_data, "--output", corrupt_output)
    assert result.returncode != 0
    assert not corrupt_output.exists()


def test_backup_requires_key_for_encrypted_settings_without_creating_archive(tmp_path):
    data = tmp_path / "encrypted"
    data.mkdir()
    _database(data / "lexcite.sqlite3", encrypted=True)
    output = tmp_path / "encrypted.zip"

    result = _run(BACKUP, "--data", data, "--output", output)

    assert result.returncode != 0
    assert "secret.key" in (result.stderr + result.stdout)
    assert not output.exists()


@pytest.mark.parametrize(
    "setting_name,settings",
    [
        ("search_settings", {"api_key_encrypted": "token"}),
        ("retrieval_settings", {"reranker": {"api_key_encrypted": "token"}}),
    ],
)
def test_backup_requires_key_for_search_and_retrieval_secrets(tmp_path, setting_name, settings):
    data = tmp_path / setting_name
    data.mkdir()
    _database(data / "lexcite.sqlite3")
    with sqlite3.connect(data / "lexcite.sqlite3") as connection:
        connection.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?)",
            (setting_name, json.dumps(settings)),
        )
    output = tmp_path / f"{setting_name}.zip"

    result = _run(BACKUP, "--data", data, "--output", output)

    assert result.returncode != 0
    assert "secret.key" in (result.stderr + result.stdout)
    assert not output.exists()


def test_backup_rejects_key_that_cannot_decrypt_saved_secret(tmp_path):
    data = tmp_path / "wrong-key"
    data.mkdir()
    _database(data / "lexcite.sqlite3")
    correct_key = Fernet.generate_key()
    token = Fernet(correct_key).encrypt(b"fixture-secret").decode()
    with sqlite3.connect(data / "lexcite.sqlite3") as connection:
        connection.execute(
            "INSERT INTO kv(key, value) VALUES (?, ?)",
            ("search_settings", json.dumps({"api_key_encrypted": token})),
        )
    (data / "secret.key").write_bytes(Fernet.generate_key())
    output = tmp_path / "wrong-key.zip"

    result = _run(BACKUP, "--data", data, "--output", output)

    assert result.returncode != 0
    assert "cannot decrypt" in (result.stderr + result.stdout)
    assert not output.exists()


def test_interrupted_backup_leaves_no_final_archive_and_allows_retry(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _database(data / "lexcite.sqlite3")
    key = Fernet.generate_key()
    (data / "secret.key").write_bytes(key)
    output = tmp_path / "backup.zip"
    injection = (
        "import runpy, sys, zipfile; "
        "script, data, output = sys.argv[1:]; "
        "sys.argv = [script, '--data', data, '--output', output]; "
        "original = zipfile.ZipFile.write; "
        "zipfile.ZipFile.write = lambda *a, **k: (_ for _ in ()).throw(OSError('simulated write interruption')); "
        "runpy.run_path(script, run_name='__main__')"
    )
    interrupted = subprocess.run(
        [sys.executable, "-c", injection, str(BACKUP), str(data), str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert interrupted.returncode != 0
    assert not output.exists()

    retried = _run(BACKUP, "--data", data, "--output", output)

    assert retried.returncode == 0, retried.stderr
    assert output.is_file()
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {"lexcite.sqlite3", "secret.key", "backup.json"}
        assert archive.read("secret.key") == key
        with archive.open("lexcite.sqlite3") as db_stream:
            restored_db = tmp_path / "backup-content.sqlite3"
            restored_db.write_bytes(db_stream.read())
        with sqlite3.connect(restored_db) as connection:
            assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
