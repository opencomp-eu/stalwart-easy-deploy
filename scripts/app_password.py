#!/usr/bin/env python3
"""Create a Stalwart app password for a mailbox.

IMAP and SMTP clients that cannot use Kanidm OIDC sign in with this secret.
The recovery admin creates it on the mailbox's behalf; the secret is printed
once and cannot be read again.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from scripts.apply import (
    JMAP_USING,
    _jmap,
    _jmap_ok,
    load_config,
    load_or_create_secrets,
)
from scripts.groupware import list_mailboxes
from scripts.mailboxes import local_part

Jmap = Callable[..., dict]


def account_email(account: dict[str, Any]) -> str:
    return str(account.get("emailAddress") or account.get("email") or "").strip().casefold()


def find_account(accounts: list[dict[str, Any]], mailbox: str) -> dict[str, Any]:
    """Match a mailbox by full address or local part."""
    wanted = mailbox.strip().casefold()
    if not wanted:
        raise ValueError("mailbox address is required")
    wanted_local = local_part(wanted) if "@" in wanted else wanted
    wanted_domain = wanted.split("@", 1)[1] if "@" in wanted else ""
    matches: list[dict[str, Any]] = []
    for account in accounts:
        email = account_email(account)
        name = str(account.get("name") or "").strip().casefold()
        if wanted_domain:
            if email == wanted or (name == wanted_local and email.endswith("@" + wanted_domain)):
                matches.append(account)
            continue
        if email == wanted or name == wanted or (email and local_part(email) == wanted_local):
            matches.append(account)
    if not matches:
        raise LookupError(
            f"No Stalwart mailbox for {mailbox!r}. "
            "Confirm the person is in Kanidm's mail-users group and re-run apply "
            "(or wait for the five-minute sync)."
        )
    if len(matches) > 1:
        emails = ", ".join(sorted({account_email(item) or str(item.get("name") or "") for item in matches}))
        raise LookupError(f"Several mailboxes match {mailbox!r}: {emails}. Pass the full address.")
    return matches[0]


def create_app_password(
    jmap: Jmap,
    account_id: str,
    *,
    description: str,
) -> str:
    """Create an app password on account_id. Returns the secret once."""
    label = description.strip() or "Mail client"
    response = jmap(
        [
            [
                "x:AppPassword/set",
                {
                    "accountId": account_id,
                    "create": {
                        "p1": {
                            "description": label,
                            "permissions": {"@type": "Inherit"},
                            "allowedIps": {},
                        }
                    },
                },
                "0",
            ]
        ],
        using=JMAP_USING,
    )
    payload = _jmap_ok(response, "0")
    not_created = payload.get("notCreated") or {}
    if not_created:
        detail = not_created.get("p1") or not_created
        raise RuntimeError(f"Could not create the app password: {detail}")
    created = (payload.get("created") or {}).get("p1") or {}
    secret = str(created.get("secret") or "").strip()
    if not secret:
        raise RuntimeError(
            "Stalwart created the app password but did not return the secret. "
            "Delete it in Account → Credentials → App Passwords and try again."
        )
    return secret


def create_for_mailbox(
    mailbox: str,
    *,
    description: str = "Mail client",
    config: dict | None = None,
    secrets: dict | None = None,
    jmap: Jmap | None = None,
) -> tuple[str, str]:
    """Return (email, secret) for the mailbox."""
    cfg = config if config is not None else load_config()
    secs = secrets if secrets is not None else load_or_create_secrets(cfg)

    def admin_jmap(calls: list, using: list[str] | None = None) -> dict:
        if jmap is not None:
            return jmap(calls, using=using)
        return _jmap(cfg, secs, calls, using=using or JMAP_USING, timeout=30)

    accounts = list_mailboxes(admin_jmap)
    account = find_account(accounts, mailbox)
    account_id = str(account.get("id") or "")
    if not account_id:
        raise RuntimeError(f"Stalwart account for {mailbox!r} has no id")
    email = account_email(account) or mailbox.strip().casefold()
    secret = create_app_password(admin_jmap, account_id, description=description)
    return email, secret


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a Stalwart app password for IMAP/SMTP clients"
    )
    parser.add_argument(
        "mailbox",
        help="Mailbox address (alice@example.com) or local part when unique",
    )
    parser.add_argument(
        "--description",
        default="Mail client",
        help="Label shown in Account → Credentials → App Passwords (default: Mail client)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print {\"email\", \"secret\"} instead of plain text",
    )
    args = parser.parse_args(argv)
    try:
        email, secret = create_for_mailbox(args.mailbox, description=args.description)
    except (FileNotFoundError, LookupError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"email": email, "secret": secret}))
    else:
        print(f"Mailbox: {email}")
        print(f"App password: {secret}")
        print("Copy it now; Stalwart will not show it again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
