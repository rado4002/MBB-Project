"""Run the Products -> Stock -> Inbox browser regression in disposable services."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
TESTS = (
    BACKEND / "tests" / "e2e" / "test_products_stock_inbox.py",
    BACKEND / "tests" / "e2e" / "test_operator_order_proposal.py",
)


def run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def free_ports(count: int) -> list[int]:
    listeners: list[socket.socket] = []
    try:
        for _ in range(count):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listeners.append(listener)
        return [listener.getsockname()[1] for listener in listeners]
    finally:
        for listener in listeners:
            listener.close()


def wait_for_postgres(container: str, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "exec", container, "pg_isready", "-U", "mbb_e2e", "-d", "mbb"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError("Disposable PostgreSQL did not become ready")


def wait_for_url(
    url: str,
    *,
    insecure: bool = False,
    expected_status: int = 200,
    expected_error_code: str | None = None,
    timeout: float = 45,
) -> None:
    context = ssl._create_unverified_context() if insecure else None
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, context=context, timeout=2) as response:
                status = response.status
                body = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read()
        except Exception as exc:  # service readiness intentionally retries
            last_error = str(exc)
            time.sleep(0.5)
            continue
        code = None
        if expected_error_code is not None:
            try:
                code = json.loads(body)["error"]["code"]
            except (ValueError, KeyError, TypeError):
                pass
        if status == expected_status and code == expected_error_code:
            return
        last_error = f"HTTP {status}, error code {code!r}"
        time.sleep(0.5)
    raise RuntimeError(f"Runtime did not become ready at {url}: {last_error}")


def api_bind_host() -> str:
    if not sys.platform.startswith("linux"):
        return "127.0.0.1"
    # host-gateway in the Nginx container resolves to this bridge address.
    gateway = subprocess.check_output(
        ["docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"],
        cwd=ROOT,
        text=True,
    ).strip()
    return str(ipaddress.IPv4Address(gateway))


def remove_container(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", name],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-frontend-build",
        action="store_true",
        help="Reuse frontend/dist after an explicit build in the same checkout.",
    )
    args = parser.parse_args()

    for test in TESTS:
        if not test.is_file():
            raise RuntimeError(f"Missing browser test: {test}")
    run(["docker", "info"])
    if not args.skip_frontend_build:
        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError("npm is required to build the React application")
        run([npm, "run", "build"], cwd=FRONTEND)
    if not (FRONTEND / "dist" / "index.html").is_file():
        raise RuntimeError("frontend/dist is missing; run without --skip-frontend-build")

    suffix = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    postgres_name = f"mbb-e2e-postgres-{suffix}"
    redis_name = f"mbb-e2e-redis-{suffix}"
    nginx_name = f"mbb-e2e-nginx-{suffix}"
    postgres_port, redis_port, api_port, https_port = free_ports(4)
    api_host = api_bind_host()
    base_url = f"https://localhost:{https_port}"
    containers = [nginx_name, redis_name, postgres_name]
    api_process: subprocess.Popen[bytes] | None = None

    with tempfile.TemporaryDirectory(prefix="mbb-products-stock-inbox-") as temporary:
        temp = Path(temporary)
        api_log = temp / "api.log"
        nginx_config = temp / "nginx.conf"
        certificate = temp / "cert.pem"
        private_key = temp / "key.pem"
        nginx_config.write_text(
            """events {}
http {
  include /etc/nginx/mime.types;
  server {
    listen 443 ssl;
    server_name localhost;
    ssl_certificate /etc/nginx/certs/cert.pem;
    ssl_certificate_key /etc/nginx/certs/key.pem;
    root /usr/share/nginx/html;
    index index.html;
    location /api/ {
      proxy_pass http://host.docker.internal:API_PORT;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
    location / { try_files $uri $uri/ /index.html; }
  }
}
""".replace("API_PORT", str(api_port)),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env.update(
            {
                "APP_ENV": "development",
                "DEBUG": "false",
                "PYTHONPATH": str(BACKEND),
                "POSTGRES_HOST": "127.0.0.1",
                "POSTGRES_PORT": str(postgres_port),
                "POSTGRES_DB": "mbb",
                "POSTGRES_USER": "mbb_e2e",
                "POSTGRES_PASSWORD": "mbb_e2e_password",
                "REDIS_HOST": "127.0.0.1",
                "REDIS_PORT": str(redis_port),
                "CELERY_BROKER_URL": f"redis://127.0.0.1:{redis_port}/0",
                "CELERY_RESULT_BACKEND": f"redis://127.0.0.1:{redis_port}/1",
                "BROWSER_AUTH_ENABLED": "true",
                "BROWSER_ALLOWED_ORIGIN": base_url,
                "BROWSER_SESSION_HMAC_SECRET": "e2e-session-secret-0123456789abcdef",
                "BROWSER_CSRF_HMAC_SECRET": "e2e-csrf-secret-0123456789abcdef012",
                "BROWSER_IDEMPOTENCY_HMAC_SECRET": "e2e-idempotency-secret-0123456789ab",
                "JWT_SECRET": "e2e-jwt-secret-0123456789abcdef012345",
                "AI_ADAPTER": "disabled",
                "AI_TURN_PROVIDER": "disabled",
                "WHATSAPP_SEND_ENABLED": "false",
                "CRM_SEND_ENABLED": "false",
                "PAYMENT_SEND_ENABLED": "false",
                "RELANCE_ENABLED": "false",
                "SCHEDULED_TASKS_ENABLED": "false",
                "M1_MAPS_FANOUT_ENABLED": "false",
                "MBB_E2E_BASE_URL": base_url,
                "AI2B_TEST_DATABASE_URL": (
                    f"postgresql+asyncpg://mbb_e2e:mbb_e2e_password@127.0.0.1:{postgres_port}/mbb"
                ),
            }
        )

        try:
            run(
                [
                    "docker", "run", "-d", "--rm", "--name", postgres_name,
                    "-p", f"127.0.0.1:{postgres_port}:5432",
                    "-e", "POSTGRES_DB=mbb", "-e", "POSTGRES_USER=mbb_e2e",
                    "-e", "POSTGRES_PASSWORD=mbb_e2e_password",
                    "-v", f"{ROOT / 'scripts' / 'init_db.sql'}:/docker-entrypoint-initdb.d/00-init.sql:ro",
                    "postgres:16-alpine",
                ]
            )
            run(
                [
                    "docker", "run", "-d", "--rm", "--name", redis_name,
                    "-p", f"127.0.0.1:{redis_port}:6379", "redis:7-alpine",
                    "redis-server", "--save", "", "--appendonly", "no",
                ]
            )
            wait_for_postgres(postgres_name)
            run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env)

            openssl = shutil.which("openssl")
            if openssl is None:
                raise RuntimeError("openssl is required to create the disposable HTTPS certificate")
            run(
                [
                    openssl, "req", "-x509", "-nodes", "-newkey", "rsa:2048",
                    "-keyout", str(private_key), "-out", str(certificate), "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                ]
            )

            log_handle = api_log.open("wb")
            api_process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", api_host, "--port", str(api_port)],
                cwd=BACKEND,
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            wait_for_url(
                f"http://{api_host}:{api_port}/api/v1/auth/session",
                expected_status=401,
                expected_error_code="session_required",
            )
            run(
                [
                    "docker", "run", "-d", "--rm", "--name", nginx_name,
                    "--add-host", "host.docker.internal:host-gateway",
                    "-p", f"127.0.0.1:{https_port}:443",
                    "-v", f"{FRONTEND / 'dist'}:/usr/share/nginx/html:ro",
                    "-v", f"{nginx_config}:/etc/nginx/nginx.conf:ro",
                    "-v", f"{certificate}:/etc/nginx/certs/cert.pem:ro",
                    "-v", f"{private_key}:/etc/nginx/certs/key.pem:ro",
                    "nginx:1.25-alpine",
                ]
            )
            wait_for_url(base_url, insecure=True)
            wait_for_url(
                f"{base_url}/api/v1/auth/session",
                insecure=True,
                expected_status=401,
                expected_error_code="session_required",
            )
            run(
                [sys.executable, "-m", "pytest", *(str(test) for test in TESTS), "-q", "--tb=short"],
                cwd=BACKEND,
                env=env,
            )
            run(
                [sys.executable, "-m", "pytest", "tests/test_business_orders_api.py", "-q", "--tb=short"],
                cwd=BACKEND, env=env,
            )
            return 0
        except Exception:
            if api_log.exists():
                print("\n--- FastAPI log tail ---", file=sys.stderr)
                print(api_log.read_text(encoding="utf-8", errors="replace")[-8000:], file=sys.stderr)
            raise
        finally:
            if api_process is not None and api_process.poll() is None:
                api_process.terminate()
                try:
                    api_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    api_process.kill()
                    api_process.wait(timeout=5)
            if "log_handle" in locals():
                log_handle.close()
            for container in containers:
                remove_container(container)


if __name__ == "__main__":
    raise SystemExit(main())
