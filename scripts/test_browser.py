"""Start a disposable fixture API and run real browser regression checks.

Install Playwright + Chromium separately. No browser dependency is needed in production.
"""
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import httpx


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as temp:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = os.environ.copy()
        for key in ("MODEL_PROVIDER", "MODEL_BASE_URL", "MODEL_NAME", "MODEL_API_KEY"):
            env.pop(key, None)
        test_url = f"http://127.0.0.1:{port}"
        env.update({
            "LEXCITE_DATA": temp,
            "LEXCITE_ADMIN_PASSWORD": secrets.token_urlsafe(24),
            "LEXCITE_PUBLIC_URL": test_url,
        })
        with open(Path(temp)/"server.log", "w+") as log:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "browser_fixture_app:app", "--app-dir", "tests", "--host", "127.0.0.1", "--port", str(port)], cwd=root, env=env, stdout=log, stderr=log)
            try:
                with httpx.Client(trust_env=False) as client:
                    for _ in range(100):
                        try:
                            if client.get(f"http://127.0.0.1:{port}/healthz").status_code == 200:
                                break
                        except httpx.ConnectError:
                            time.sleep(.05)
                    else:
                        raise RuntimeError("Fixture server did not start")
                env.update({
                    "LEXCITE_TEST_URL": test_url,
                    "LEXCITE_TEST_PASSWORD": env["LEXCITE_ADMIN_PASSWORD"],
                })
                result = subprocess.run(["node", os.getenv("LEXCITE_BROWSER_SCRIPT", "tests/browser_smoke.cjs")], cwd=root, env=env, timeout=90)
                if result.returncode:
                    log.seek(0)
                    print(log.read())
                raise SystemExit(result.returncode)
            finally:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
