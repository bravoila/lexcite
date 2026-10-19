"""Validation shared by the standalone backup and restore commands."""
import json

from cryptography.fernet import Fernet, InvalidToken


def encrypted_api_keys(connection):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kv'"
    ).fetchone():
        raise ValueError("database is missing the required kv table")

    encrypted = []
    for setting_name in ("settings", "retrieval_settings", "search_settings"):
        row = connection.execute("SELECT value FROM kv WHERE key=?", (setting_name,)).fetchone()
        if row is None:
            continue
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"database {setting_name} is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"database {setting_name} is not an object")
        configs = value.values() if setting_name == "retrieval_settings" else (value,)
        for config in configs:
            if not isinstance(config, dict):
                raise ValueError(f"database {setting_name} contains an invalid service configuration")
            token = config.get("api_key_encrypted")
            if token:
                if not isinstance(token, str):
                    raise ValueError(f"database {setting_name} contains an invalid encrypted key")
                encrypted.append(token)
    return encrypted


def validate_encryption_key(key, encrypted_values):
    if key is None:
        if encrypted_values:
            raise ValueError(
                "database contains encrypted settings but secret.key is missing; "
                "restore would not be able to decrypt them"
            )
        return
    try:
        cipher = Fernet(key)
        for token in encrypted_values:
            cipher.decrypt(token.encode("ascii")).decode("utf-8")
    except (TypeError, ValueError, UnicodeError, InvalidToken) as exc:
        raise ValueError(
            "secret.key is invalid or cannot decrypt the database's encrypted settings"
        ) from exc
