#!/usr/bin/env python3
"""Kanidm contact sync and default calendar/address-book names for Stalwart."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KANIDM_ROOT = PROJECT_ROOT.parent / "kanidm-easy-deploy"
STATE_DIR = PROJECT_ROOT / ".stalwart-easy-deploy"
LOCK_PATH = STATE_DIR / "contacts-sync.lock"

ADDRESS_BOOK_NAME = "Address Book"
CALENDAR_NAME = "Calendar"
STALWART_ADDRESS_BOOK = "Stalwart Address Book"
STALWART_CALENDAR = "Stalwart Calendar"
KANIDM_UID_PREFIX = "urn:opencomp:kanidm:"
CONTACTS_UNIT = "stalwart-easy-deploy-contacts"
USER_USING = [
    "urn:ietf:params:jmap:core",
    "urn:ietf:params:jmap:contacts",
    "urn:ietf:params:jmap:calendars",
]
SETTINGS_USING = [
    "urn:ietf:params:jmap:core",
    "urn:stalwart:jmap",
]


@dataclass(frozen=True)
class Person:
    uuid: str
    name: str
    display_name: str
    emails: tuple[str, ...]

    @property
    def uid(self) -> str:
        return f"{KANIDM_UID_PREFIX}{self.uuid}"


@dataclass
class MailboxSync:
    address_book_renamed: bool = False
    calendar_renamed: bool = False
    created: int = 0
    updated: int = 0
    removed: int = 0


def _is_false(value: Any) -> bool:
    if value is False:
        return True
    return str(value or "").strip().lower() in {"false", "no", "0"}


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def contacts_sync_enabled(config: dict) -> bool:
    """Kanidm people are copied into mailbox address books unless opted out."""
    identity = config.get("identity") if isinstance(config.get("identity"), dict) else {}
    if str(identity.get("provider") or "").strip().lower() != "kanidm":
        return False
    if _is_false(identity.get("managed")):
        return False
    contacts = identity.get("contacts", True)
    if isinstance(contacts, dict):
        if _is_false(contacts.get("managed")):
            return False
        return _to_bool(contacts.get("enabled", True))
    return _to_bool(contacts)


def mail_domain(config: dict) -> str:
    return str((config.get("stalwart") or {}).get("domain") or "").strip().lower()


def account_labels(account_name: str, domain: str) -> set[str]:
    name = account_name.strip()
    labels = {name}
    if "@" in name:
        labels.add(name.split("@", 1)[0])
    elif domain:
        labels.add(f"{name}@{domain}")
    return {label for label in labels if label}


def login_account(name: str, domain: str) -> str:
    """Full address Stalwart can impersonate. Fallback-admin login does not add a domain."""
    account = name.strip().lower()
    if "@" not in account and domain:
        account = f"{account}@{domain}"
    return account


def impersonation_username(account: str, admin: str) -> str:
    """Stalwart master-user login: target first, fallback admin after '%'."""
    target = account.strip()
    master = admin.strip()
    if not target or not master:
        raise ValueError("impersonation requires an account and the recovery admin name")
    if "%" in target or "%" in master:
        raise ValueError("account names used for impersonation cannot contain '%'")
    return f"{target}%{master}"


def is_stock_collection_name(
    name: str,
    *,
    stalwart_base: str,
    plain_base: str,
    labels: set[str],
) -> bool:
    """True for the auto-created Stalwart name, including the account suffix."""
    text = name.strip()
    if text in {stalwart_base, plain_base}:
        return True
    folded = {label.casefold() for label in labels if label}
    for base in (stalwart_base, plain_base):
        prefix = f"{base} ("
        if not (text.startswith(prefix) and text.endswith(")")):
            continue
        inner = text[len(prefix) : -1].strip()
        # "Stalwart …" is only ever the product default. The plain name with a
        # suffix is the same default after defaultDisplayName is changed, and
        # only when the suffix is this account.
        if base == stalwart_base or inner.casefold() in folded:
            return True
    return False


def desired_collection_name(
    name: str,
    *,
    stalwart_base: str,
    plain_base: str,
    labels: set[str],
) -> str | None:
    if not is_stock_collection_name(
        name, stalwart_base=stalwart_base, plain_base=plain_base, labels=labels
    ):
        return None
    if name.strip() == plain_base:
        return None
    return plain_base


def _attr_values(entry: dict, key: str) -> list[str]:
    value = entry.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = []
        for item in value:
            if isinstance(item, str):
                items.append(item)
            elif isinstance(item, dict):
                inner = item.get("value") or item.get("address")
                if isinstance(inner, str):
                    items.append(inner)
    else:
        return []
    return [item.strip() for item in items if item and item.strip()]


def parse_kanidm_people(payload: Any) -> list[Person]:
    """Parse `kanidm person list --output json` into people who have an email."""
    if isinstance(payload, str):
        text = payload.strip()
        start = text.find("[")
        if start < 0:
            raise ValueError("Kanidm person list did not return a JSON array")
        payload = json.loads(text[start:])
    if isinstance(payload, dict):
        payload = payload.get("entries") or payload.get("result") or []
    if not isinstance(payload, list):
        raise ValueError("Kanidm person list JSON must be an array")
    people: list[Person] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        entry = item.get("attrs") if isinstance(item.get("attrs"), dict) else item
        if not isinstance(entry, dict):
            continue
        classes = {value.casefold() for value in _attr_values(entry, "class")}
        if "service_account" in classes and "person" not in classes:
            continue
        uuid = next(iter(_attr_values(entry, "uuid")), "")
        name = next(iter(_attr_values(entry, "name")), "")
        emails = tuple(dict.fromkeys(email.casefold() for email in _attr_values(entry, "mail")))
        if not uuid or not emails or uuid in seen:
            continue
        display = next(iter(_attr_values(entry, "displayname")), "") or name or emails[0]
        seen.add(uuid)
        people.append(Person(uuid=uuid, name=name, display_name=display, emails=emails))
    return people


def owner_emails(account_name: str, people: list[Person], domain: str) -> set[str]:
    account = login_account(account_name, domain)
    local = account.split("@", 1)[0]
    owned = {account}
    for person in people:
        ident = {person.name.casefold(), *(email.casefold() for email in person.emails)}
        ident |= {email.split("@", 1)[0] for email in person.emails}
        if account in ident or local in ident:
            owned.update(email.casefold() for email in person.emails)
    return owned


def contact_card(person: Person, address_book_id: str) -> dict:
    emails = {
        str(index): {"address": email, "contexts": {"work": True}}
        for index, email in enumerate(person.emails)
    }
    return {
        "@type": "Card",
        "kind": "individual",
        "uid": person.uid,
        "name": {"full": person.display_name},
        "emails": emails,
        "addressBookIds": {address_book_id: True},
    }


def _card_signature(card: dict) -> tuple:
    name = str(((card.get("name") or {}) if isinstance(card.get("name"), dict) else {}).get("full") or "").strip()
    emails = card.get("emails") if isinstance(card.get("emails"), dict) else {}
    addresses = tuple(
        sorted(
            str((item or {}).get("address") or "").strip().casefold()
            for item in emails.values()
            if isinstance(item, dict) and str((item or {}).get("address") or "").strip()
        )
    )
    return name, addresses


def plan_contact_changes(
    people: list[Person],
    existing: list[dict],
    address_book_id: str,
    owner: set[str],
) -> tuple[dict[str, dict], dict[str, dict], list[str]]:
    """Create, update, and destroy directory cards. Personal cards are untouched."""
    desired: dict[str, Person] = {}
    for person in people:
        if owner.intersection(email.casefold() for email in person.emails):
            continue
        desired[person.uid] = person
    existing_by_uid: dict[str, dict] = {}
    for card in existing:
        if not isinstance(card, dict):
            continue
        uid = str(card.get("uid") or "")
        if uid.startswith(KANIDM_UID_PREFIX) and card.get("id"):
            existing_by_uid[uid] = card
    create: dict[str, dict] = {}
    update: dict[str, dict] = {}
    destroy: list[str] = []
    for uid, person in desired.items():
        card = contact_card(person, address_book_id)
        current = existing_by_uid.get(uid)
        if current is None:
            create[f"kanidm-{person.uuid}"] = card
            continue
        if _card_signature(current) != _card_signature(card):
            update[str(current["id"])] = {"name": card["name"], "emails": card["emails"]}
    for uid, card in existing_by_uid.items():
        if uid not in desired:
            destroy.append(str(card["id"]))
    return create, update, destroy


def _method_ok(response: dict, call_id: str) -> dict:
    for entry in response.get("methodResponses") or []:
        if not isinstance(entry, list) or len(entry) < 3 or entry[2] != call_id:
            continue
        name, payload = entry[0], entry[1]
        if name == "error" or str(name).endswith("/error"):
            detail = payload if isinstance(payload, dict) else {"description": payload}
            raise RuntimeError(detail.get("description") or detail.get("type") or str(payload))
        if not isinstance(payload, dict):
            raise RuntimeError(f"unexpected JMAP payload for {call_id}: {payload!r}")
        return payload
    raise RuntimeError(f"no JMAP response for {call_id}")


def _reject_set_errors(payload: dict) -> dict:
    for key in ("notCreated", "notUpdated", "notDestroyed"):
        failed = payload.get(key) or {}
        if failed:
            raise RuntimeError(f"JMAP {key}: {failed}")
    return payload


def _query_ids(jmap, method: str, args: dict) -> list[str]:
    ids: list[str] = []
    position = 0
    while True:
        page = dict(args)
        page["position"] = position
        page["limit"] = 200
        payload = _method_ok(jmap([[method, page, "q"]]), "q")
        batch = [str(item) for item in (payload.get("ids") or []) if item]
        ids.extend(batch)
        total = payload.get("total")
        position += len(batch)
        if (
            not batch
            or (isinstance(total, int) and position >= total)
            or len(batch) < 200
            or position > 10000
        ):
            return ids


def _get_objects(jmap, method: str, ids: list[str], args: dict | None = None) -> list[dict]:
    found: list[dict] = []
    extra = dict(args or {})
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        payload = _method_ok(jmap([[method, {**extra, "ids": chunk}, "g"]]), "g")
        found.extend(item for item in (payload.get("list") or []) if isinstance(item, dict))
    return found


def _apply_set(jmap, method: str, args: dict) -> dict:
    payload = _method_ok(jmap([[method, args, "s"]]), "s")
    return _reject_set_errors(payload)


def sync_mailbox(jmap, *, account_id: str, account_name: str, domain: str, people: list[Person] | None) -> MailboxSync:
    """Rename stock collections and, when people is set, upsert Kanidm contacts.

    `jmap` must already be authenticated as this mailbox. Stalwart stores the
    collection display name on the caller's own preference row.
    """
    stats = MailboxSync()
    labels = account_labels(account_name, domain)
    base = {"accountId": account_id}

    book_ids = _query_ids(jmap, "AddressBook/query", base)
    books = _get_objects(jmap, "AddressBook/get", book_ids, base) if book_ids else []
    book = _choose_address_book(books, labels)
    if book is None:
        created = _apply_set(
            jmap,
            "AddressBook/set",
            {**base, "create": {"directory": {"name": ADDRESS_BOOK_NAME}}},
        )
        created_book = (created.get("created") or {}).get("directory") or {}
        book_id = str(created_book.get("id") or "")
        if not book_id:
            raise RuntimeError(f"AddressBook/set did not return an id for {account_name}")
    else:
        book_id = str(book.get("id") or "")
        renamed = desired_collection_name(
            str(book.get("name") or ""),
            stalwart_base=STALWART_ADDRESS_BOOK,
            plain_base=ADDRESS_BOOK_NAME,
            labels=labels,
        )
        if renamed and book_id:
            _apply_set(jmap, "AddressBook/set", {**base, "update": {book_id: {"name": renamed}}})
            stats.address_book_renamed = True

    calendar_ids = _query_ids(jmap, "Calendar/query", base)
    calendars = _get_objects(jmap, "Calendar/get", calendar_ids, base) if calendar_ids else []
    calendar_id = _choose_calendar(calendars, labels)
    if calendar_id:
        current = next((item for item in calendars if str(item.get("id") or "") == calendar_id), {})
        renamed = desired_collection_name(
            str(current.get("name") or ""),
            stalwart_base=STALWART_CALENDAR,
            plain_base=CALENDAR_NAME,
            labels=labels,
        )
        if renamed:
            _apply_set(jmap, "Calendar/set", {**base, "update": {calendar_id: {"name": renamed}}})
            stats.calendar_renamed = True

    if people is None or not book_id:
        return stats

    card_ids = _query_ids(jmap, "ContactCard/query", base)
    cards = _get_objects(jmap, "ContactCard/get", card_ids, base) if card_ids else []
    create, update, destroy = plan_contact_changes(
        people, cards, book_id, owner_emails(account_name, people, domain)
    )
    if create or update or destroy:
        _apply_contacts(jmap, base, create, update, destroy)
    stats.created = len(create)
    stats.updated = len(update)
    stats.removed = len(destroy)
    return stats


def _choose_address_book(books: list[dict], labels: set[str]) -> dict | None:
    named = [
        book
        for book in books
        if str(book.get("name") or "").strip() == ADDRESS_BOOK_NAME and book.get("id")
    ]
    if named:
        return named[0]
    stock = [
        book
        for book in books
        if book.get("id")
        and is_stock_collection_name(
            str(book.get("name") or ""),
            stalwart_base=STALWART_ADDRESS_BOOK,
            plain_base=ADDRESS_BOOK_NAME,
            labels=labels,
        )
    ]
    if stock:
        return stock[0]
    defaults = [book for book in books if book.get("isDefault") is True and book.get("id")]
    if defaults:
        return defaults[0]
    if len(books) == 1 and books[0].get("id"):
        return books[0]
    return None


def _choose_calendar(calendars: list[dict], labels: set[str]) -> str:
    stock = [
        item
        for item in calendars
        if item.get("id")
        and is_stock_collection_name(
            str(item.get("name") or ""),
            stalwart_base=STALWART_CALENDAR,
            plain_base=CALENDAR_NAME,
            labels=labels,
        )
        and str(item.get("name") or "").strip() != CALENDAR_NAME
    ]
    if not stock:
        return ""
    defaults = [item for item in stock if item.get("isDefault") is True]
    chosen = (defaults or stock)[0]
    return str(chosen.get("id") or "")


def _apply_contacts(jmap, base: dict, create: dict, update: dict, destroy: list[str]) -> None:
    create_items = list(create.items())
    update_items = list(update.items())
    destroy_items = list(destroy)
    while create_items or update_items or destroy_items:
        args = dict(base)
        if create_items:
            args["create"] = dict(create_items[:50])
            del create_items[:50]
        if update_items:
            args["update"] = dict(update_items[:50])
            del update_items[:50]
        if destroy_items:
            args["destroy"] = destroy_items[:50]
            del destroy_items[:50]
        _apply_set(jmap, "ContactCard/set", args)


def list_mailboxes(jmap) -> list[dict]:
    """User accounts already materialised in Stalwart (management API)."""
    ids = _query_ids(jmap, "x:Account/query", {})
    if not ids:
        return []
    accounts = _get_objects(jmap, "x:Account/get", ids)
    mailboxes = []
    for account in accounts:
        kind = str(account.get("@type") or "User").strip().lower()
        if kind and kind != "user":
            continue
        if account.get("id"):
            mailboxes.append(account)
    return mailboxes


def mailbox_login(account: dict, domain: str) -> str:
    email = str(account.get("emailAddress") or account.get("email") or "").strip()
    if "@" in email:
        return email.casefold()
    return login_account(str(account.get("name") or ""), domain)


def apply_default_names(jmap) -> None:
    for method, name in (
        ("x:AddressBook/set", ADDRESS_BOOK_NAME),
        ("x:Calendar/set", CALENDAR_NAME),
    ):
        _reject_set_errors(
            _method_ok(
                jmap([[method, {"update": {"singleton": {"defaultDisplayName": name}}}, "n"]]),
                "n",
            )
        )
    try:
        _reject_set_errors(
            _method_ok(
                jmap(
                    [
                        [
                            "x:Action/set",
                            {"create": {"reload-settings": {"@type": "ReloadSettings"}}},
                            "r",
                        ]
                    ]
                ),
                "r",
            )
        )
    except RuntimeError as exc:
        print(f"  Warning: could not reload Stalwart settings: {exc}", file=sys.stderr)


def _load_yaml(path: Path) -> dict:
    with path.open() as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a mapping")
    return data


def _kanidm_deploy() -> tuple[dict, str]:
    deploy_path = KANIDM_ROOT / "deploy.yaml"
    secrets_path = KANIDM_ROOT / ".kanidm-easy-deploy" / "secrets.yaml"
    if not deploy_path.is_file():
        raise RuntimeError(
            f"Kanidm contact sync needs {deploy_path}. "
            "Apply kanidm-easy-deploy on this host, or set identity.contacts: false."
        )
    config = _load_yaml(deploy_path)
    kanidm = config.get("kanidm") if isinstance(config.get("kanidm"), dict) else {}
    password = ""
    if secrets_path.is_file():
        password = str(_load_yaml(secrets_path).get("IDM_ADMIN_PASSWORD") or "")
    return kanidm, password


def _kanidm_cli(*args: str, password: str = "") -> subprocess.CompletedProcess[str]:
    kanidm, _stored = _kanidm_deploy()
    data_dir = Path(str(kanidm.get("data_dir") or "/var/lib/kanidm"))
    tools = (
        f"{kanidm.get('tools_image', 'docker.io/kanidm/tools')}:"
        f"{kanidm.get('tools_tag', kanidm.get('tag', '1.11.1'))}"
    )
    config_file = data_dir / "kanidm-client-config"
    tokens = data_dir / "kanidm_tokens"
    if not config_file.is_file():
        raise RuntimeError(f"Missing Kanidm client config {config_file}. Run kanidm apply.sh first.")
    if not tokens.is_file():
        tokens.write_text('{"instances": {}}\n')
        tokens.chmod(0o600)
    cmd = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--network",
        "kanidm-net",
        "-v",
        f"{config_file}:/root/.config/kanidm:ro",
        "-v",
        f"{tokens}:/root/.cache/kanidm_tokens",
    ]
    if password:
        cmd.extend(["-e", f"KANIDM_PASSWORD={password}"])
    cmd.extend([tools, "kanidm", *args])
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def fetch_kanidm_people() -> list[Person]:
    _kanidm, password = _kanidm_deploy()
    listed = _kanidm_cli("person", "list", "--name", "idm_admin", "--output", "json")
    if listed.returncode != 0:
        if not password:
            raise RuntimeError(
                "Could not list Kanidm people and IDM_ADMIN_PASSWORD is missing. "
                f"{(listed.stderr or listed.stdout or '').strip()[:400]}"
            )
        login = _kanidm_cli("login", "--name", "idm_admin", password=password)
        if login.returncode != 0:
            detail = (login.stderr or login.stdout or "").strip()[:400]
            raise RuntimeError(f"Kanidm idm_admin login failed: {detail}")
        listed = _kanidm_cli("person", "list", "--name", "idm_admin", "--output", "json")
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout or "").strip()[:400]
        raise RuntimeError(f"Could not list Kanidm people: {detail}")
    return parse_kanidm_people(listed.stdout)


def _stalwart_running() -> bool:
    return subprocess.run(["docker", "inspect", "stalwart"], capture_output=True).returncode == 0


@contextmanager
def sync_lock(*, blocking: bool) -> Iterator[bool]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a")
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _merge_identity(config: dict) -> dict:
    from scripts.apply import apply_engine_identity_sidecar

    apply_engine_identity_sidecar(config)
    return config


def sync_groupware(config: dict, secrets: dict) -> str:
    """Set collection names, then mirror Kanidm people into each mailbox."""
    from scripts.apply import _jmap, recovery_admin_user

    _merge_identity(config)

    if not _stalwart_running():
        raise RuntimeError("stalwart container is not running")
    domain = mail_domain(config)
    admin = recovery_admin_user(config)

    def admin_jmap(calls: list, using: list[str] | None = None) -> dict:
        return _jmap(config, secrets, calls, using=using or SETTINGS_USING, timeout=30)

    apply_default_names(admin_jmap)
    accounts = list_mailboxes(admin_jmap)
    people: list[Person] | None = None
    people_error: Exception | None = None
    if contacts_sync_enabled(config):
        try:
            people = fetch_kanidm_people()
        except (RuntimeError, ValueError) as exc:
            people_error = exc

    totals = MailboxSync()
    mailboxes = 0
    failures: list[str] = []
    seen: set[str] = set()
    for account in accounts:
        account_id = str(account.get("id") or "")
        account_name = mailbox_login(account, domain)
        if not account_id or not account_name or account_name in seen:
            continue
        if account_name == admin.strip().lower():
            continue
        seen.add(account_name)
        username = impersonation_username(account_name, admin)

        def user_jmap(calls: list, username: str = username) -> dict:
            return _jmap(config, secrets, calls, username=username, using=USER_USING, timeout=30)

        try:
            stats = sync_mailbox(
                user_jmap,
                account_id=account_id,
                account_name=account_name,
                domain=domain,
                people=people,
            )
        except RuntimeError as exc:
            failures.append(f"{account_name}: {exc}")
            continue
        mailboxes += 1
        totals.address_book_renamed += int(stats.address_book_renamed)
        totals.calendar_renamed += int(stats.calendar_renamed)
        totals.created += stats.created
        totals.updated += stats.updated
        totals.removed += stats.removed

    parts = [
        f"names set to {ADDRESS_BOOK_NAME!r} and {CALENDAR_NAME!r}",
        f"{mailboxes} mailbox{'es' if mailboxes != 1 else ''}",
    ]
    if totals.address_book_renamed or totals.calendar_renamed:
        parts.append(
            f"renamed {totals.address_book_renamed} address book"
            f"{'s' if totals.address_book_renamed != 1 else ''} and "
            f"{totals.calendar_renamed} calendar{'s' if totals.calendar_renamed != 1 else ''}"
        )
    if people is not None:
        parts.append(
            f"contacts +{totals.created} ~{totals.updated} -{totals.removed} "
            f"from {len(people)} Kanidm people"
        )
    message = "Groupware: " + "; ".join(parts)
    if failures:
        message += ". Failed: " + "; ".join(failures[:5])
    print(f"  {message}")
    if people_error is not None:
        raise RuntimeError(str(people_error)) from people_error
    if failures and mailboxes == 0:
        raise RuntimeError(message)
    return message


def sync_groupware_locked(config: dict, secrets: dict) -> str:
    with sync_lock(blocking=True) as acquired:
        if not acquired:
            return "Groupware sync skipped because another sync holds the lock."
        return sync_groupware(config, secrets)


def systemd_unit_dir() -> Path:
    return Path(os.environ.get("EASYDEPLOY_SYSTEMD_UNIT_DIR", "/etc/systemd/system"))


def sync_exec_start(project_root: Path) -> str:
    python = project_root / ".venv" / "bin" / "python"
    if python.is_file():
        return f"{python} -m scripts.groupware"
    uv = shutil.which("uv") or "uv"
    return f"{uv} run --project {project_root} python -m scripts.groupware"


def render_sync_service(project_root: Path) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Sync Kanidm people into Stalwart address books",
            "Wants=network-online.target docker.service",
            "After=network-online.target docker.service",
            "",
            "[Service]",
            "Type=oneshot",
            f"WorkingDirectory={project_root}",
            f"ExecStart={sync_exec_start(project_root)}",
            "",
        ]
    )


def render_sync_timer(unit_name: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Sync Kanidm people into Stalwart address books every 5 minutes",
            "",
            "[Timer]",
            "OnCalendar=*:0/5",
            "Persistent=true",
            f"Unit={unit_name}.service",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )


def _systemd_available() -> bool:
    if os.environ.get("EASYDEPLOY_ASSUME_SYSTEMD") == "1":
        return True
    return Path("/run/systemd/system").exists()


def _requires_privilege(unit_dir: Path) -> bool:
    if os.geteuid() == 0:
        return False
    if unit_dir.exists():
        return not os.access(unit_dir, os.W_OK)
    return not os.access(unit_dir.parent, os.W_OK)


def _run_systemctl(run_command, args: list[str], *, privileged: bool, check: bool) -> None:
    command = ["systemctl", *args]
    if privileged:
        if not shutil.which("sudo"):
            raise RuntimeError("Installing the contact sync timer requires sudo when run as a non-root user")
        command.insert(0, "sudo")
    run_command(command, check=check)


def _write_unit(path: Path, content: str, *, privileged: bool) -> bool:
    if not privileged:
        if path.exists() and path.read_text() == content:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return True
    if path.exists():
        try:
            if path.read_text() == content:
                return False
        except PermissionError:
            pass
    import tempfile

    with tempfile.NamedTemporaryFile("w", delete=False) as temporary:
        temporary.write(content)
        source = Path(temporary.name)
    try:
        subprocess.run(["sudo", "install", "-m", "0644", str(source), str(path)], check=True)
    finally:
        source.unlink(missing_ok=True)
    return True


def _remove_unit(path: Path, *, privileged: bool) -> None:
    if not path.exists():
        return
    if privileged:
        subprocess.run(["sudo", "rm", "-f", str(path)], check=True)
        return
    path.unlink()


def reconcile_contacts_timer(
    config: dict,
    *,
    project_root: Path | None = None,
    unit_dir: Path | None = None,
    run_command=subprocess.run,
    unit_name: str = CONTACTS_UNIT,
) -> str:
    """Install or remove the 5-minute Kanidm contact sync timer."""
    root = project_root or PROJECT_ROOT
    unit_dir = unit_dir or systemd_unit_dir()
    _merge_identity(config)
    service_path = unit_dir / f"{unit_name}.service"
    timer_path = unit_dir / f"{unit_name}.timer"
    enabled = contacts_sync_enabled(config)

    if enabled:
        if not _systemd_available():
            raise RuntimeError("Automatic Kanidm contact sync requires systemd on this host")
        try:
            unit_dir.mkdir(parents=True, exist_ok=True)
            privileged = _requires_privilege(unit_dir)
            service_changed = _write_unit(
                service_path, render_sync_service(root), privileged=privileged
            )
            timer_changed = _write_unit(
                timer_path, render_sync_timer(unit_name), privileged=privileged
            )
            _run_systemctl(run_command, ["daemon-reload"], privileged=privileged, check=True)
            _run_systemctl(
                run_command,
                ["enable", "--now", f"{unit_name}.timer"],
                privileged=privileged,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"Failed to install Kanidm contact sync timer: {exc}") from exc
        if service_changed or timer_changed:
            return "Kanidm contact sync timer installed or updated."
        return "Kanidm contact sync timer already up to date."

    if service_path.exists() or timer_path.exists():
        if not _systemd_available():
            raise RuntimeError("Cannot remove Kanidm contact sync timer because systemd is unavailable")
        try:
            privileged = _requires_privilege(unit_dir)
            _run_systemctl(
                run_command,
                ["disable", "--now", f"{unit_name}.timer"],
                privileged=privileged,
                check=False,
            )
            _remove_unit(timer_path, privileged=privileged)
            _remove_unit(service_path, privileged=privileged)
            _run_systemctl(run_command, ["daemon-reload"], privileged=privileged, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"Failed to remove Kanidm contact sync timer: {exc}") from exc
        return "Kanidm contact sync timer removed."
    return "Kanidm contact sync timer not configured."


def reconcile_contacts_schedule(config: dict) -> None:
    try:
        message = reconcile_contacts_timer(config)
    except (OSError, RuntimeError) as exc:
        print(f"Contact sync: {exc}", file=sys.stderr)
        return
    print(f"Contact sync: {message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Kanidm people into Stalwart address books")
    parser.parse_args(argv)
    with sync_lock(blocking=False) as acquired:
        if not acquired:
            print("Contact sync already running.")
            return 0
        from scripts.apply import SECRETS_PATH, load_config, load_yaml

        config = load_config()
        if not SECRETS_PATH.is_file():
            print(f"Error: missing {SECRETS_PATH}. Run apply.sh first.", file=sys.stderr)
            return 1
        try:
            sync_groupware(config, load_yaml(SECRETS_PATH))
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
