"""Run DOM integration against a disposable HTTP server in the same process environment."""
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
        for k in (
            "MODEL_PROFILE", "MODEL_PROVIDER", "MODEL_BASE_URL", "MODEL_NAME", "MODEL_API_KEY",
            "MODEL_LOCAL_PROVIDER", "MODEL_LOCAL_BASE_URL", "MODEL_LOCAL_NAME", "MODEL_LOCAL_API_KEY",
        ):
            env.pop(k, None)
        env.update({
            "LEXCITE_DATA": temp,
            "LEXCITE_ADMIN_PASSWORD": secrets.token_urlsafe(24),
            "MODEL_PROFILE": "local",
            "MODEL_LOCAL_PROVIDER": "none",
        })
        with open(Path(temp)/"server.log", "w+") as log:
            process = subprocess.Popen([sys.executable,"-m","uvicorn","app.main:app","--host","127.0.0.1","--port",str(port)],cwd=root,env=env,stdout=log,stderr=log)
            try:
                with httpx.Client(trust_env=False) as c:
                    for _ in range(100):
                        try:
                            if c.get(f"http://127.0.0.1:{port}/healthz").status_code == 200:
                                break
                        except httpx.ConnectError:
                            time.sleep(.05)
                    else:
                        raise RuntimeError("Test server failed to start")
                env.update({"LEXCITE_TEST_URL":f"http://127.0.0.1:{port}", "LEXCITE_TEST_PASSWORD":env["LEXCITE_ADMIN_PASSWORD"]})
                result = subprocess.run(["node","tests/ui_smoke.cjs"],cwd=root,env=env,timeout=60)
                if result.returncode:
                    log.seek(0)
                    print(log.read())
                raise SystemExit(result.returncode)
            finally:
                process.terminate()
                process.wait(timeout=5)

if __name__ == "__main__":
    main()
