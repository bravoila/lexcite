"""Consistent SQLite backup including the encryption key; output is confidential."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import zipfile

p = argparse.ArgumentParser()
p.add_argument("--data", default=os.getenv("LEXCITE_DATA", "data"))
p.add_argument("--output", required=True)
args = p.parse_args()
data, out = Path(args.data), Path(args.output)
if not (data / "lexcite.sqlite3").exists():
    raise SystemExit("No database found")
if out.exists():
    raise SystemExit("Refusing to overwrite an existing backup")
out.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as tmp:
    target = Path(tmp) / "lexcite.sqlite3"
    with sqlite3.connect(data / "lexcite.sqlite3") as source, sqlite3.connect(target) as dest:
        source.backup(dest)
        dest.execute("DELETE FROM sessions")
        dest.commit()
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(target, "lexcite.sqlite3")
        if (data / "secret.key").exists():
            z.write(data / "secret.key", "secret.key")
        z.writestr("backup.json", json.dumps({"version": "1.3.0", "created": dt.datetime.now(dt.timezone.utc).isoformat()}))
print(str(out.resolve()))
