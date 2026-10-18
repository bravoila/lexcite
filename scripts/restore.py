"""Offline restore into an empty data directory; stop the service first."""
import argparse
from pathlib import Path
import zipfile

p = argparse.ArgumentParser()
p.add_argument("archive")
p.add_argument("--data", default="data")
args = p.parse_args()
data = Path(args.data)
data.mkdir(parents=True, exist_ok=True)
if any(data.glob("*.sqlite3*")) or (data / "secret.key").exists():
    raise SystemExit("Restore requires an empty data directory. Stop the service and move the old directory first.")
with zipfile.ZipFile(args.archive) as z:
    for name in ("lexcite.sqlite3", "secret.key"):
        if name in z.namelist():
            target = data / name
            target.write_bytes(z.read(name))
            target.chmod(0o600)
print("Restored. Start the service and log in again.")
