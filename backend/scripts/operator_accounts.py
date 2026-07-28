"""Controlled local commands for future operator account administration.

These commands do not authenticate the operating-system caller. Except for the
one-time empty-database bootstrap, every mutation requires an explicit active
Administrator account ID plus a reviewable authorization reference and reason.
Run only from an access-controlled administrative console.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import async_session_factory  # noqa: E402
from app.operator_identity.accounts import (
    AdministrativeAuthorization,
    bootstrap_first_administrator,
    disable_operator_account,
    provision_operator_account,
    reactivate_operator_account,
    reset_operator_password,
)  # noqa: E402
from app.operator_identity.browser_sessions import BrowserSessionStore  # noqa: E402


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--email")


def _add_authorization_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--authorized-by-account-id", required=True, type=uuid.UUID)
    parser.add_argument("--authorization-reference", required=True)
    parser.add_argument("--authorization-reason", required=True)


def _authorization(args: argparse.Namespace) -> AdministrativeAuthorization:
    return AdministrativeAuthorization(
        actor_account_id=args.authorized_by_account_id,
        authorization_reference=args.authorization_reference,
        reason=args.authorization_reason,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser("bootstrap-first-administrator")
    _add_identity_arguments(bootstrap)
    bootstrap.add_argument("--human-operator-label", required=True)

    provision = subparsers.add_parser("provision-operator-account")
    _add_identity_arguments(provision)
    _add_authorization_arguments(provision)
    provision.add_argument(
        "--role",
        required=True,
        choices=("administrator", "operator", "analyst"),
    )

    for command in (
        "reset-operator-password",
        "disable-operator-account",
        "reactivate-operator-account",
    ):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--username", required=True)
        _add_authorization_arguments(command_parser)
    return parser


async def _run(args: argparse.Namespace) -> str | None:
    temporary_credential: str | None = None
    async with async_session_factory() as session:
        async with session.begin():
            if args.command == "bootstrap-first-administrator":
                issued = await bootstrap_first_administrator(
                    session,
                    username=args.username,
                    display_name=args.display_name,
                    email=args.email,
                    human_operator_label=args.human_operator_label,
                )
                temporary_credential = issued.consume()
            elif args.command == "provision-operator-account":
                issued = await provision_operator_account(
                    session,
                    authorization=_authorization(args),
                    username=args.username,
                    display_name=args.display_name,
                    email=args.email,
                    role=args.role,
                )
                temporary_credential = issued.consume()
            else:
                store = BrowserSessionStore()
                if args.command == "reset-operator-password":
                    issued = await reset_operator_password(
                        session,
                        store,
                        authorization=_authorization(args),
                        username=args.username,
                    )
                    temporary_credential = issued.consume()
                elif args.command == "disable-operator-account":
                    await disable_operator_account(
                        session,
                        store,
                        authorization=_authorization(args),
                        username=args.username,
                    )
                elif args.command == "reactivate-operator-account":
                    issued = await reactivate_operator_account(
                        session,
                        store,
                        authorization=_authorization(args),
                        username=args.username,
                    )
                    temporary_credential = issued.consume()
                else:  # pragma: no cover - argparse constrains this branch.
                    raise ValueError("unsupported command")
    return temporary_credential


def main() -> int:
    args = build_parser().parse_args()
    try:
        credential = asyncio.run(_run(args))
    except Exception as exc:  # The CLI emits only exception type, never secrets.
        print(f"Command failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if credential is not None:
        print(f"Temporary credential (displayed once): {credential}")
    else:
        print("Command completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
