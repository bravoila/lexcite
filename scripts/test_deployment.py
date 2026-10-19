"""Check an isolated release image behind real TLS Nginx without model calls.

Requires Docker, OpenSSL and the development Python dependencies. Creates and
removes its own containers, network and data volume; never mounts project data.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="lexcite:deployment-test")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    version = json.loads((root / "package.json").read_text())["version"]
    prefix = "lexcite-deployment-" + secrets.token_hex(5)
    containers, checks = [], []
    network = volume = None

    def docker(*command, check=True):
        result = subprocess.run(["docker", *command], capture_output=True, text=True)
        if check and result.returncode:
            raise RuntimeError(f"Docker {command[0]} failed: {result.stderr.strip()}")
        return result

    def create(*command):
        cid = docker("create", *command).stdout.strip()
        containers.append(cid)
        docker("start", cid)
        return cid

    def remove(cid):
        docker("rm", "-f", cid)
        containers.remove(cid)

    def wait_app(cid):
        for _ in range(100):
            probe = docker(
                "exec", cid, "python", "-c",
                "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200",
                check=False,
            )
            if not probe.returncode:
                return
            time.sleep(.1)
        raise RuntimeError("Disposable app never became healthy")

    def wait_proxy(connection):
        for _ in range(100):
            try:
                if connection.get("/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        raise RuntimeError("Disposable HTTPS proxy did not become responsive")

    def client(base):
        return httpx.Client(
            base_url=base, verify=False, trust_env=False, timeout=15,
            headers={"X-Lexcite": "1"},
        )

    try:
        defaults = {}
        for line in (root / ".env.example").read_text().splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                defaults[key] = value
        network = docker("network", "create", prefix).stdout.strip()
        volume = docker("volume", "create", prefix).stdout.strip()
        with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
            directory = Path(temporary)
            key, cert = directory / "key.pem", directory / "cert.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(cert)],
                capture_output=True, check=True,
            )
            key.chmod(0o600)
            config = directory / "nginx.conf"
            config.write_text("""events {}
http {
    server {
        listen 443 ssl;
        server_name localhost;
        ssl_certificate /etc/nginx/smoke-cert.pem;
        ssl_certificate_key /etc/nginx/smoke-key.pem;
        add_header Strict-Transport-Security "max-age=31536000" always;
        client_max_body_size 20m;
        location / {
            proxy_pass http://lexcite:8080;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-For $remote_addr;
            proxy_read_timeout 420s;
        }
    }
}
""")

            def app(trust):
                command = [
                    "--name", prefix+"-app", "--network", network,
                    "--network-alias", "lexcite", "--network-alias", "reranker",
                    "--network-alias", "searxng", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp",
                    "--mount", f"type=volume,src={volume},dst=/app/data",
                ]
                env = defaults | {
                    "LEXCITE_TRUST_PROXY": str(trust), "LEXCITE_LOCAL_RECOVERY": "0",
                    "LEXCITE_PUBLIC_URL": "https://localhost", "LEXCITE_SECURE_COOKIE": "0",
                }
                for name, value in env.items():
                    command += ["--env", name+"="+value]
                cid = create(*command, args.image)
                wait_app(cid)
                return cid

            app_id = app(0)
            published = json.loads(docker("inspect", app_id).stdout)[0]["NetworkSettings"]["Ports"]
            assert all(value is None for value in published.values())
            checks.append("backend-not-published")
            docker(
                "exec", app_id, "python", "-c",
                "import socket, ipaddress; assert ipaddress.ip_address(socket.gethostbyname('reranker')).is_private",
            )
            checks.append("disabled-default-reranker-with-real-private-dns-starts")
            code = docker("exec", app_id, "cat", "/app/data/setup-code.txt").stdout.strip()
            assert len(code) > 10
            assert docker("exec", app_id, "stat", "-c", "%a", "/app/data/setup-code.txt").stdout.strip() == "600"
            logs = docker("logs", app_id)
            assert code not in logs.stdout + logs.stderr
            checks.append("setup-code-private-file-not-logs")

            proxy_id = create(
                "--name", prefix+"-proxy", "--network", network, "-p", "127.0.0.1::443",
                "--mount", f"type=bind,src={config},dst=/etc/nginx/nginx.conf,readonly",
                "--mount", f"type=bind,src={cert},dst=/etc/nginx/smoke-cert.pem,readonly",
                "--mount", f"type=bind,src={key},dst=/etc/nginx/smoke-key.pem,readonly",
                "nginx:stable-alpine",
            )
            proxy_info = json.loads(docker("inspect", proxy_id).stdout)[0]
            port = proxy_info["NetworkSettings"]["Ports"]["443/tcp"][0]["HostPort"]
            base = "https://127.0.0.1:" + port
            password = secrets.token_urlsafe(24)
            with client(base) as authenticated:
                wait_proxy(authenticated)
                setup = authenticated.post("/api/setup", json={"setup_code": code, "password": password})
                assert setup.status_code == 200, setup.status_code
                docker("exec", app_id, "test", "!", "-e", "/app/data/setup-code.txt")
                assert docker("exec", app_id, "stat", "-c", "%a", "/app/data/lexcite.sqlite3").stdout.strip() == "600"
                checks.append("real-first-run-setup-deletes-code-and-protects-database")

                for trust, attempts in ((0, 30), (1, 100)):
                    if trust:
                        remove(app_id)
                        app_id = app(1)
                        docker("exec", proxy_id, "nginx", "-s", "reload")
                        wait_proxy(authenticated)
                    with client(base) as anonymous:
                        for number in range(attempts):
                            forged = "127.0.0.1" if number % 2 == 0 else f"198.51.100.{number % 250+1}"
                            response = anonymous.post(
                                "/api/login", headers={"X-Forwarded-For": forged},
                                json={"password": "deliberately-wrong-password"},
                            )
                            assert response.status_code == (401 if number < 10 else 429), (trust, number, response.status_code)
                            if number >= 10:
                                assert int(response.headers["retry-after"]) > 0
                    response = authenticated.post("/api/login", json={"password": password})
                    assert response.status_code == 200
                    assert "Secure" in response.headers["set-cookie"] and "HttpOnly" in response.headers["set-cookie"]
                    assert response.headers["strict-transport-security"] == "max-age=31536000"
                    assert response.headers["cache-control"] == "no-store"
                    checks.append(f"trust-proxy-{trust}-{attempts}-forged-failures-never-lock-valid-password")
                    session = authenticated.get("/api/session").json()
                    assert session["authenticated"] and session["version"] == version
                    status = authenticated.get("/api/status")
                    assert status.status_code == 200 and not status.json()["retrieval"].get("configuration_error")
                    if not trust:
                        case = authenticated.post("/api/cases", json={
                            "title": "Disposable deployment smoke", "event_date": "2025-01-01",
                        })
                        assert case.status_code == 200
                        case_id = case.json()["id"]
                        answer = authenticated.post(f"/api/cases/{case_id}/ask", json={
                            "text": "Synthetic extract-only query", "mode": "extract",
                        })
                        assert answer.status_code == 200 and answer.json()["model_calls"] == 0
                        docker("exec", app_id, "python", "-c", """
import httpx
with httpx.Client(trust_env=False, headers={'X-Lexcite':'1'}) as connection:
    for number in range(12):
        response = connection.post('http://127.0.0.1:8080/api/login',
                                   headers={'X-Forwarded-For':f'198.51.100.{number+1}'},
                                   json={'password':'deliberately-wrong-password'})
        assert response.status_code == (401 if number < 10 else 429), (number, response.status_code)
""")
                        checks.append("real-loopback-no-default-exemption-and-uvicorn-ignores-forged-xff")
                    else:
                        saved = authenticated.get(f"/api/cases/{case_id}").json()
                        assert len(saved["messages"]) == 2
                        checks.append("restart-retains-cases-and-atomic-message-pair")
                for url in (f"/assets/{version}/app.js", f"/assets/{version}/style.css"):
                    response = authenticated.get(url)
                    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
                    assert response.content == (root / "static" / Path(url).name).read_bytes()
                checks.append("release-assets-through-real-tls-proxy")
                response = authenticated.post("/api/login", json={"password": "x"*17000})
                assert response.status_code == 413 and len(response.content) < 1000
                assert response.headers["x-content-type-options"] == "nosniff" and response.headers["cache-control"] == "no-store"
                checks.append("early-login-error-retains-protection-and-small-body")
                assert authenticated.post("/api/retrieval/settings", json={
                    "auto_index": False, "embedding": {"provider": "none"},
                    "reranker": {"provider": "custom", "base_url": "http://reranker:8081/v1", "model": "no-model-call"},
                }).status_code == 200
                assert authenticated.post("/api/search/settings", json={
                    "provider": "searxng", "base_url": "http://searxng:8080",
                }).status_code == 200
                checks.append("documented-private-service-names-save-in-container")
            result = {
                "passed": len(checks), "checks": checks, "version": version, "image": args.image,
                "image_id": json.loads(docker("image", "inspect", args.image).stdout)[0]["Id"],
                "nginx_image_id": proxy_info["Image"], "self_signed_test_tls": True,
                "real_model_calls": 0, "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
    finally:
        errors = []
        for command in (
            [("rm", "-f", cid) for cid in reversed(containers)]
            + ([("volume", "rm", volume)] if volume else [])
            + ([("network", "rm", network)] if network else [])
        ):
            cleanup = docker(*command, check=False)
            if cleanup.returncode:
                errors.append(cleanup.stderr.strip())
        if errors:
            raise RuntimeError("Disposable resource cleanup failed: " + "; ".join(errors))
    result["cleanup_complete"] = True
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
