"""Start, inspect, and stop a disposable localhost Orders UX review runtime.

This foundation creates no Orders. Run from the repository root with Python 3.12.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
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
from datetime import UTC, datetime
from pathlib import Path

from run_products_stock_inbox_e2e import ROOT, BACKEND, FRONTEND, api_bind_host

STATE = Path(tempfile.gettempdir()) / "mbb-orders-ux-review" / "state.json"
LABEL = "mbb.orders-ux-review.id"
KINDS = ("postgres", "redis", "nginx")


def command(*args: str, **kwargs) -> str:
    return subprocess.check_output(args, text=True, cwd=ROOT, **kwargs).strip()


def check_environment() -> None:
    if STATE.exists():
        raise RuntimeError(f"Review state already exists: {STATE}; use status or stop")
    if any(name.startswith("mbb-orders-ux-") for name in command("docker", "ps", "-a", "--format", "{{.Names}}").splitlines()):
        raise RuntimeError("Untracked Orders UX containers exist; inspect them before starting")
    if not (ROOT / "scripts" / "init_db.sql").is_file():
        raise RuntimeError("Missing disposable database initialization")
    if not shutil.which("openssl"):
        raise RuntimeError("OpenSSL is required for local HTTPS")
    if not shutil.which("npm"):
        raise RuntimeError("npm is required")


def reserve_ports(count: int) -> list[int]:
    sockets = [socket.socket() for _ in range(count)]
    try:
        for item in sockets:
            item.bind(("127.0.0.1", 0))
        return [item.getsockname()[1] for item in sockets]
    finally:
        for item in sockets:
            item.close()


def owned_container(name: str, expected_id: str, token: str) -> bool:
    try:
        actual = command("docker", "inspect", "--format", "{{.Id}} {{index .Config.Labels \"" + LABEL + "\"}}", name)
    except subprocess.CalledProcessError:
        return False
    parts = actual.split()
    if len(parts) != 2 or parts[0] != expected_id or parts[1] != token:
        raise RuntimeError(f"Container ownership mismatch: {name}")
    return True


def stop_owned(state: dict) -> None:
    token = state["token"]
    # Validate every existing target before stopping any resource.
    existing = [name for name, identity in state["containers"].items() if owned_container(name, identity, token)]
    pid = state.get("api_pid")
    if pid:
        import psutil
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - state["api_created_at"]) > 2 or "uvicorn" not in " ".join(process.cmdline()):
                raise RuntimeError("API process ownership mismatch")
        except psutil.NoSuchProcess:
            process = None
    else:
        process = None
    if process:
        process.terminate()
        try:
            process.wait(10)
        except psutil.TimeoutExpired:
            process.kill()
    for name in existing:
        subprocess.run(["docker", "rm", "-f", name], check=True, cwd=ROOT, stdout=subprocess.DEVNULL)
    runtime = Path(state["runtime_dir"]).resolve()
    allowed = STATE.parent.resolve()
    if runtime.parent != allowed or runtime.name != token:
        raise RuntimeError("Runtime directory ownership mismatch")
    shutil.rmtree(runtime)
    STATE.unlink(missing_ok=True)


def wait_url(url: str, *, insecure: bool = False, timeout: int = 60) -> None:
    context = ssl._create_unverified_context() if insecure else None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2, context=context) as response:
                if response.status == 200:
                    return
        except urllib.error.HTTPError as error:
            if error.code == 401 and url.endswith("/api/v1/auth/session"):
                return
        except (OSError, TimeoutError):
            pass
        time.sleep(.5)
    raise RuntimeError(f"Review service did not become ready: {url}")


def seed_accounts(environment: dict[str, str], password: str) -> None:
    code = """import asyncio, uuid
from datetime import datetime, UTC
from app.database import async_session_factory, engine
from app.models.operator_account import OperatorAccount
from app.operator_identity.passwords import hash_password
async def main():
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        for role in ('administrator', 'operator'):
            session.add(OperatorAccount(account_id=uuid.uuid4(), username_normalized=f'uxreview.{role}', display_name=f'Fictional UX Review {role.title()}', password_hash=hash_password(__import__('os').environ['MBB_UX_REVIEW_PASSWORD']), role=role, status='active', must_change_password=False, password_changed_at=now))
        await session.commit()
    await engine.dispose()
asyncio.run(main())"""
    subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env={**environment, "MBB_UX_REVIEW_PASSWORD": password}, check=True)


def start() -> None:
    check_environment()
    token = uuid.uuid4().hex
    postgres_port, redis_port, api_port, https_port = reserve_ports(4)
    runtime = STATE.parent / token
    runtime.mkdir(parents=True, exist_ok=False)
    state = {"token": token, "runtime_dir": str(runtime), "containers": {}, "url": f"https://localhost:{https_port}/business/orders"}
    password = secrets.token_urlsafe(24)
    try:
        subprocess.run([shutil.which("npm") or "npm", "run", "build"], cwd=FRONTEND, check=True)
        for kind, image, options, arguments in (
            ("postgres", "postgres:16-alpine", ["-p", f"127.0.0.1:{postgres_port}:5432", "-e", "POSTGRES_DB=mbb", "-e", "POSTGRES_USER=mbb_ux", "-e", "POSTGRES_PASSWORD=mbb_ux_disposable", "-v", f"{ROOT / 'scripts' / 'init_db.sql'}:/docker-entrypoint-initdb.d/00-init.sql:ro"], []),
            ("redis", "redis:7-alpine", ["-p", f"127.0.0.1:{redis_port}:6379"], ["redis-server", "--save", "", "--appendonly", "no"]),
        ):
            name = f"mbb-orders-ux-{kind}-{token}"
            identity = command("docker", "run", "-d", "--rm", "--name", name, "--label", f"{LABEL}={token}", *options, image, *arguments)
            state["containers"][name] = identity
        deadline = time.monotonic() + 60
        postgres_name = f"mbb-orders-ux-postgres-{token}"
        while time.monotonic() < deadline:
            if subprocess.run(["docker", "exec", postgres_name, "pg_isready", "-U", "mbb_ux", "-d", "mbb"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(.5)
        else:
            raise RuntimeError("Disposable PostgreSQL did not become ready")
        origin = f"https://localhost:{https_port}"
        environment = {**os.environ, "APP_ENV": "development", "DEBUG": "false", "PYTHONPATH": str(BACKEND), "POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": str(postgres_port), "POSTGRES_DB": "mbb", "POSTGRES_USER": "mbb_ux", "POSTGRES_PASSWORD": "mbb_ux_disposable", "REDIS_HOST": "127.0.0.1", "REDIS_PORT": str(redis_port), "CELERY_BROKER_URL": f"redis://127.0.0.1:{redis_port}/0", "CELERY_RESULT_BACKEND": f"redis://127.0.0.1:{redis_port}/1", "BROWSER_AUTH_ENABLED": "true", "BROWSER_ALLOWED_ORIGIN": origin, "BROWSER_SESSION_HMAC_SECRET": secrets.token_hex(32), "BROWSER_CSRF_HMAC_SECRET": secrets.token_hex(32), "BROWSER_IDEMPOTENCY_HMAC_SECRET": secrets.token_hex(32), "JWT_SECRET": secrets.token_hex(32), "AI_ADAPTER": "disabled", "AI_TURN_PROVIDER": "disabled", "WHATSAPP_SEND_ENABLED": "false", "CRM_SEND_ENABLED": "false", "PAYMENT_SEND_ENABLED": "false", "RELANCE_ENABLED": "false", "SCHEDULED_TASKS_ENABLED": "false", "M1_MAPS_FANOUT_ENABLED": "false"}
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=environment, check=True)
        seed_accounts(environment, password)
        certificate, private_key, nginx_config = (runtime / name for name in ("cert.pem", "key.pem", "nginx.conf"))
        subprocess.run(["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-keyout", str(private_key), "-out", str(certificate), "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        nginx_config.write_text("events {}\nhttp { include /etc/nginx/mime.types; server { listen 443 ssl; server_name localhost; ssl_certificate /etc/nginx/certs/cert.pem; ssl_certificate_key /etc/nginx/certs/key.pem; root /usr/share/nginx/html; index index.html; location /api/ { proxy_pass http://host.docker.internal:" + str(api_port) + "; proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto https; } location / { try_files $uri $uri/ /index.html; } } }\n", encoding="utf-8")
        log = (runtime / "api.log").open("wb")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
        api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", api_bind_host(), "--port", str(api_port)], cwd=BACKEND, env=environment, stdout=log, stderr=subprocess.STDOUT, creationflags=creationflags)
        log.close()
        import psutil
        state["api_pid"] = api.pid
        state["api_created_at"] = psutil.Process(api.pid).create_time()
        wait_url(f"http://{api_bind_host()}:{api_port}/api/v1/auth/session")
        name = f"mbb-orders-ux-nginx-{token}"
        identity = command("docker", "run", "-d", "--rm", "--name", name, "--label", f"{LABEL}={token}", "--add-host", "host.docker.internal:host-gateway", "-p", f"127.0.0.1:{https_port}:443", "-v", f"{FRONTEND / 'dist'}:/usr/share/nginx/html:ro", "-v", f"{nginx_config}:/etc/nginx/nginx.conf:ro", "-v", f"{certificate}:/etc/nginx/certs/cert.pem:ro", "-v", f"{private_key}:/etc/nginx/certs/key.pem:ro", "nginx:1.25-alpine")
        state["containers"][name] = identity
        wait_url(origin, insecure=True)
        wait_url(origin + "/api/v1/auth/session", insecure=True)
        state["accounts"] = ["uxreview.administrator", "uxreview.operator"]
        state["password"] = password
        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        print(f"Review URL: {state['url']}\nAccounts: {', '.join(state['accounts'])}\nPassword: {password}\nOrders: none (foundation only)\nStop: python scripts/orders_ux_review.py stop")
    except BaseException:
        stop_owned(state)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop"))
    action = parser.parse_args().action
    if action == "start":
        start()
        return
    if not STATE.exists():
        raise RuntimeError("No Orders UX review runtime is registered")
    state = json.loads(STATE.read_text(encoding="utf-8"))
    for name, identity in state["containers"].items():
        owned_container(name, identity, state["token"])
    if action == "status":
        print(f"Review URL: {state['url']}\nAccounts: {', '.join(state['accounts'])}\nOrders: none (foundation only)")
    else:
        stop_owned(state)
        print("Stopped only registered Orders UX review resources")


if __name__ == "__main__":
    main()
