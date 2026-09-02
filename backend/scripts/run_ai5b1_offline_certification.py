"""Create, certify, and destroy the isolated AI-5B1 PostgreSQL runtime."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from collections.abc import Sequence
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid


BACKEND_ROOT = Path(__file__).resolve().parents[1]
TEST_PATH = "tests/test_ai5b1_offline_certification_postgres.py"
DEADLINE_TEST_PATH = "tests/test_ai5b1_deadlines.py"
AI5B2_BRIDGE_TEST_PATHS = (
    "tests/test_ai5b2_canary_bridge.py",
    "tests/test_ai5b2_canary_bridge_postgres.py",
)
REGRESSION_TEST_PATHS = (
    "tests/test_ai_offline_integration_postgres.py",
    "tests/test_ai4d_continuity_postgres.py",
    "tests/test_ai4e_postgres.py",
    "tests/test_ai_product_capabilities_postgres.py",
    "tests/test_product_offer_postgres.py",
    "tests/test_commercial_state_postgres.py",
    "tests/test_ai_handoff_postgres.py",
    "tests/test_conversation_ownership_postgres.py",
    "tests/test_ai_audit_postgres.py",
    "tests/test_ai_runtime_audit_postgres.py",
    "tests/test_operator_conversation_queries_postgres.py",
)
CLUSTER_PREFIX = "mbb-ai5b1-cluster-"
DATABASE_PREFIX = "ai5b1_cert_"
DATABASE_USER = "ai5b1_admin"


class CertificationRuntimeError(RuntimeError):
    pass


@dataclass
class DisposablePostgresRuntime:
    """One verified loopback PostgreSQL cluster owned by a single evaluation."""

    suite: str
    cluster_id: str = field(default_factory=lambda: CLUSTER_PREFIX + uuid.uuid4().hex)
    database_name: str = field(
        default_factory=lambda: DATABASE_PREFIX + uuid.uuid4().hex
    )
    port: int = field(default_factory=lambda: _unused_loopback_port())
    cluster_root: Path = field(init=False)
    data_dir: Path = field(init=False)
    log_path: Path = field(init=False)
    env: dict[str, str] = field(init=False)
    started: bool = False
    database_created: bool = False
    migrated: bool = False
    schema_verified: bool = False
    database_dropped: bool = False
    cluster_stopped: bool = False
    directory_removed: bool = False

    def __post_init__(self) -> None:
        temp_root = Path(tempfile.gettempdir()).resolve()
        self.cluster_root = (temp_root / self.cluster_id).resolve()
        if (
            self.cluster_root.parent != temp_root
            or not self.cluster_root.name.startswith(CLUSTER_PREFIX)
        ):
            raise CertificationRuntimeError("temporary cluster identity is unsafe")
        self.data_dir = self.cluster_root / "data"
        self.log_path = self.cluster_root / "postgres.log"
        self.env = _certification_environment(
            port=self.port,
            database_name=self.database_name,
            cluster_id=self.cluster_id,
        )

    @property
    def database_url(self) -> str:
        return self.env["AI5B1_TEST_DATABASE_URL"]

    def prepare(self) -> None:
        """Create, start, migrate, and verify the exact run-owned database."""
        bin_dir = _postgres_bin()
        self.cluster_root.mkdir(parents=False, exist_ok=False)
        try:
            _run(
                [
                    _exe(bin_dir, "initdb"),
                    "-D",
                    str(self.data_dir),
                    "-U",
                    DATABASE_USER,
                    "-A",
                    "trust",
                    "--encoding=UTF8",
                    "--no-locale",
                ]
            )
            _run(
                [
                    _exe(bin_dir, "pg_ctl"),
                    "-D",
                    str(self.data_dir),
                    "-l",
                    str(self.log_path),
                    "-o",
                    f"-p {self.port} -h 127.0.0.1",
                    "-w",
                    "start",
                ],
                capture=False,
            )
            self.started = True
            _run(
                [
                    _exe(bin_dir, "createdb"),
                    "-h",
                    "127.0.0.1",
                    "-p",
                    str(self.port),
                    "-U",
                    DATABASE_USER,
                    self.database_name,
                ],
                env=self.env,
            )
            self.database_created = True
            _run(
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    "-c",
                    "alembic.ini",
                    "upgrade",
                    "head",
                ],
                env=self.env,
                echo=True,
            )
            self.migrated = True
            verified = _run(
                [
                    _exe(bin_dir, "psql"),
                    "-h",
                    "127.0.0.1",
                    "-p",
                    str(self.port),
                    "-U",
                    DATABASE_USER,
                    "-d",
                    self.database_name,
                    "-X",
                    "-tAc",
                    "SELECT count(*) FROM pg_namespace WHERE nspname='mbb'",
                ],
                env=self.env,
            )
            self.schema_verified = verified.stdout.strip() == "1"
            if not self.schema_verified:
                raise CertificationRuntimeError("migrated mbb schema was not verified")
        except BaseException:
            try:
                self.cleanup()
            except BaseException:
                # Preserve the setup failure. cleanup() already attempts every
                # run-owned teardown step before it reports its own failure.
                pass
            raise

    def cleanup(self) -> None:
        """Idempotently remove only resources carrying this runtime's identity."""
        bin_dir = _postgres_bin()
        cleanup_failure: BaseException | None = None
        if self.database_created and self.started and not self.database_dropped:
            try:
                _run(
                    [
                        _exe(bin_dir, "dropdb"),
                        "-h",
                        "127.0.0.1",
                        "-p",
                        str(self.port),
                        "-U",
                        DATABASE_USER,
                        self.database_name,
                    ],
                    env=self.env,
                )
                self.database_dropped = True
            except BaseException as exc:
                cleanup_failure = exc
        if self.started and not self.cluster_stopped:
            try:
                _run(
                    [
                        _exe(bin_dir, "pg_ctl"),
                        "-D",
                        str(self.data_dir),
                        "-m",
                        "fast",
                        "-w",
                        "stop",
                    ],
                    capture=False,
                )
                self.cluster_stopped = True
            except BaseException as exc:
                cleanup_failure = cleanup_failure or exc
        temp_root = Path(tempfile.gettempdir()).resolve()
        resolved_root = self.cluster_root.resolve()
        if (
            resolved_root.exists()
            and resolved_root.parent == temp_root
            and resolved_root.name == self.cluster_id
            and resolved_root.name.startswith(CLUSTER_PREFIX)
        ):
            try:
                shutil.rmtree(resolved_root)
            except BaseException as exc:
                cleanup_failure = cleanup_failure or exc
        self.directory_removed = not resolved_root.exists()
        if cleanup_failure is not None:
            raise cleanup_failure

    def evidence(self) -> dict[str, object]:
        return {
            "suite": self.suite,
            "cluster_identity": self.cluster_id,
            "database_identity": self.database_name,
            "loopback_port": self.port,
            "loopback_only": True,
            "database_created": self.database_created,
            "migrated": self.migrated,
            "schema_verified": self.schema_verified,
            "database_dropped": self.database_dropped,
            "cluster_stopped": self.cluster_stopped,
            "temporary_directory_removed": self.directory_removed,
        }


def _postgres_bin() -> Path:
    initdb = shutil.which("initdb")
    if initdb:
        return Path(initdb).resolve().parent
    if os.name == "nt":
        root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PostgreSQL"
        candidates = sorted(root.glob("*/bin/initdb.exe"), reverse=True)
        if candidates:
            return candidates[0].parent
    raise CertificationRuntimeError("installed PostgreSQL binaries were not found")


def _exe(bin_dir: Path, name: str) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    path = bin_dir / f"{name}{suffix}"
    if not path.is_file():
        raise CertificationRuntimeError(f"required PostgreSQL binary missing: {name}")
    return str(path)


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run(
    arguments: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 180,
    echo: bool = False,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        arguments,
        cwd=BACKEND_ROOT,
        env=env,
        check=False,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if echo and capture:
        if completed.stdout:
            print(_console_safe(completed.stdout), end="")
        if completed.stderr:
            print(_console_safe(completed.stderr), end="", file=sys.stderr)
    if completed.returncode != 0:
        safe_command = Path(arguments[0]).name
        raise CertificationRuntimeError(
            f"{safe_command} failed with exit code {completed.returncode}"
        )
    return completed


def _console_safe(value: str) -> str:
    """Escape non-ASCII subprocess logs for legacy Windows console encodings."""
    return value.encode("ascii", errors="backslashreplace").decode("ascii")


def _certification_environment(
    *,
    port: int,
    database_name: str,
    cluster_id: str,
) -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if "DEEPSEEK" in name.upper():
            env.pop(name, None)
    env.update(
        {
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": str(port),
            "POSTGRES_DB": database_name,
            "POSTGRES_USER": DATABASE_USER,
            "POSTGRES_PASSWORD": "",
            "AI5B1_TEST_DATABASE_URL": (
                f"postgresql+asyncpg://{DATABASE_USER}@127.0.0.1:{port}/"
                f"{database_name}"
            ),
            "AI5B1_DISPOSABLE_CLUSTER_ID": cluster_id,
            "AI_ADAPTER": "disabled",
            "AI_TURN_PROVIDER": "disabled",
            "WHATSAPP_SEND_ENABLED": "false",
            "CRM_SEND_ENABLED": "false",
            "PAYMENT_SEND_ENABLED": "false",
            "RELANCE_ENABLED": "false",
            "SCHEDULED_TASKS_ENABLED": "false",
            "M1_MAPS_FANOUT_ENABLED": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PGCONNECT_TIMEOUT": "5",
        }
    )
    env.pop("AI3F_TEST_DATABASE_URL", None)
    env.pop("DATABASE_URL", None)
    return env


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    suite = parser.add_mutually_exclusive_group()
    suite.add_argument(
        "--regressions",
        action="store_true",
        help="run the relevant existing PostgreSQL regression set",
    )
    suite.add_argument(
        "--ai5b2-bridge",
        action="store_true",
        help="run the offline AI-5B2 real-journey bridge checks",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _arguments(argv)
    if arguments.regressions:
        suite = "postgres_regressions"
        pytest_targets = REGRESSION_TEST_PATHS
    elif arguments.ai5b2_bridge:
        suite = "ai5b2_bridge_offline"
        pytest_targets = AI5B2_BRIDGE_TEST_PATHS
    else:
        suite = "ai5b1_scenarios"
        pytest_targets = (DEADLINE_TEST_PATH, TEST_PATH)
    runtime = DisposablePostgresRuntime(suite=suite)
    env = runtime.env
    if arguments.ai5b2_bridge:
        env["AI5B2_BRIDGE_TEST_DATABASE_URL"] = env["AI5B1_TEST_DATABASE_URL"]
        env["AI5B2_BRIDGE_DISPOSABLE_CLUSTER_ID"] = runtime.cluster_id
        env["AI5B2_BRIDGE_MODE"] = "offline"
    if arguments.regressions:
        for name in (
            "AI1D_TEST_DATABASE_URL",
            "AI1E_TEST_DATABASE_URL",
            "AI2B_TEST_DATABASE_URL",
            "AI3F_TEST_DATABASE_URL",
            "AI4D_TEST_DATABASE_URL",
            "AI4E_TEST_DATABASE_URL",
            "E1_TEST_DATABASE_URL",
            "E2_TEST_DATABASE_URL",
        ):
            env[name] = env["AI5B1_TEST_DATABASE_URL"]
    tests_passed = False
    failure: BaseException | None = None
    try:
        runtime.prepare()
        _run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                *pytest_targets,
            ],
            env=env,
            timeout=600,
            echo=True,
        )
        tests_passed = True
    except BaseException as exc:
        failure = exc
    finally:
        try:
            runtime.cleanup()
        except BaseException as exc:
            failure = failure or exc

    evidence = {
        "contract_version": "mbb-ai5b-contract-v2",
        **runtime.evidence(),
        "tests_passed": tests_passed,
        "seven_scenarios_passed": (
            tests_passed
            if not arguments.regressions and not arguments.ai5b2_bridge
            else None
        ),
        "four_canary_bridge_passed": (tests_passed if arguments.ai5b2_bridge else None),
        "provider_network_calls": 0,
        "provider_api_tokens": 0,
        "provider_cost_usd": "0",
    }
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    if failure is not None:
        print(f"AI-5B1 certification runtime failed: {failure}", file=sys.stderr)
        return 1
    if not all(
        (
            runtime.database_created,
            runtime.migrated,
            runtime.schema_verified,
            tests_passed,
            runtime.database_dropped,
            runtime.cluster_stopped,
            runtime.directory_removed,
        )
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
