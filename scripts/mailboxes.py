"""Create Stalwart mailboxes for Kanidm's mail-users group, and remove them again.

Membership is the cutover switch. A person can exist in Kanidm for files and
chat without a mailbox. Joining mail-users creates the mailbox on the next
apply or the five-minute sync, before they sign in. Leaving the group removes
only mailboxes this sync has tracked, so the address can be relayed back to
Microsoft. Copy the mail out first: removal deletes it here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

MAIL_USERS_GROUP = "mail-users"
# Previous filter treated every person with a mail attribute as a mailbox.
LEGACY_MAILBOX_FILTER = "(&(|(objectclass=account)(objectclass=person))(|(mail=?)(spn=?)))"
_LOCAL_PART = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,62}$")
STATE_NAME = "provisioned-mailboxes.json"


def ldap_domain(base_dn: str) -> str:
    labels = []
    for part in str(base_dn).split(","):
        key, _, value = part.partition("=")
        if key.strip().lower() == "dc" and value.strip():
            labels.append(value.strip().lower())
    return ".".join(labels)


def kanidm_mailbox_filter(base_dn: str, kanidm_domain: str, group: str = MAIL_USERS_GROUP) -> str:
    """LDAP filterMailbox: only members of mail-users, matched by recipient address."""
    domain = kanidm_domain.strip().lower().rstrip(".")
    suffix = base_dn.strip()
    if not domain or not suffix:
        raise ValueError("mailbox filter needs a Kanidm domain and an LDAP base DN")
    group_dn = f"spn={group}@{domain},{suffix}"
    return f"(&(|(objectclass=account)(objectclass=person))(mail=?)(memberof={group_dn}))"


def mailbox_filter_for(base_dn: str, configured: str = "") -> str:
    text = configured.strip()
    if text and text != LEGACY_MAILBOX_FILTER:
        return text
    domain = ldap_domain(base_dn)
    if not domain:
        return LEGACY_MAILBOX_FILTER
    return kanidm_mailbox_filter(base_dn, domain)


def group_name(value: str) -> str:
    rdn = value.split(",", 1)[0].strip()
    if "=" in rdn:
        rdn = rdn.split("=", 1)[1].strip()
    return rdn.split("@", 1)[0].strip().casefold()


def person_in_group(person, group: str = MAIL_USERS_GROUP) -> bool:
    wanted = group.casefold()
    return any(group_name(value) == wanted for value in getattr(person, "groups", ()))


def mailbox_address(person, domain: str) -> str | None:
    suffix = "@" + domain.strip().lower()
    for email in person.emails:
        if email.casefold().endswith(suffix):
            return email.casefold()
    return None


def local_part(email: str) -> str:
    return email.split("@", 1)[0].casefold()


def plan_mailboxes(
    desired: dict[str, str],
    existing: dict[str, str],
    tracked: set[str],
    *,
    reserved: set[str],
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Return accounts to create (email → local part), to delete (email → id), and the next tracked set.

    ``desired`` and ``existing`` are email → local part and email → account id.
    Only addresses this sync has tracked are deleted, so a hand-made shared
    mailbox is left alone. ``reserved`` local parts (the recovery admin) are
    never created or deleted.
    """
    wanted = {
        email: part
        for email, part in desired.items()
        if part not in reserved and _LOCAL_PART.match(part)
    }
    present = {email: account_id for email, account_id in existing.items() if account_id}
    to_create = {email: part for email, part in wanted.items() if email not in present}
    to_delete = {
        email: present[email]
        for email in tracked
        if email in present and email not in wanted and local_part(email) not in reserved
    }
    next_tracked = (tracked | (set(wanted) & set(present)) | set(to_create)) - set(to_delete)
    return to_create, to_delete, next_tracked


def account_create_payload(local: str, domain_id: str) -> dict:
    return {
        "@type": "User",
        "name": local,
        "domainId": domain_id,
        "aliases": {},
        "credentials": {},
        "memberGroupIds": {},
        "roles": {"@type": "User"},
        "permissions": {"@type": "Inherit"},
        "quotas": {},
        "encryptionAtRest": {"@type": "Disabled"},
    }


def state_path(root: Path) -> Path:
    return root / ".stalwart-easy-deploy" / STATE_NAME


def load_tracked(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    emails = data.get("emails") if isinstance(data, dict) else None
    if not isinstance(emails, list):
        return set()
    return {str(email).strip().casefold() for email in emails if str(email).strip()}


def save_tracked(path: Path, emails: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"emails": sorted(emails)}
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _method_result(response: dict, call_id: str) -> dict:
    for entry in response.get("methodResponses") or []:
        if not isinstance(entry, list) or len(entry) < 2 or entry[2] != call_id:
            continue
        payload = entry[1]
        if isinstance(payload, dict):
            return payload
    raise RuntimeError(f"no JMAP response for {call_id}")


def provision_mail_users(config: dict, secrets: dict, people: list, jmap) -> str:
    """Create and retire Stalwart accounts so mail-users membership is the mailbox."""
    from scripts.apply import recovery_admin_user

    domain = str((config.get("stalwart") or {}).get("domain") or "").strip().lower()
    if not domain:
        return "Mailboxes: skipped, no mail domain"
    desired: dict[str, str] = {}
    for person in people:
        if not person_in_group(person):
            continue
        email = mailbox_address(person, domain)
        if email:
            desired[email] = local_part(email)
    listed = jmap([["x:Domain/query", {}, "q"], ["x:Account/query", {}, "aq"]])
    domain_ids = list(_method_result(listed, "q").get("ids") or [])
    account_ids = list(_method_result(listed, "aq").get("ids") or [])
    follow = []
    if domain_ids:
        follow.append(["x:Domain/get", {"ids": domain_ids}, "dg"])
    if account_ids:
        follow.append(["x:Account/get", {"ids": account_ids}, "ag"])
    got = jmap(follow) if follow else {"methodResponses": []}
    domain_id = ""
    for item in _method_result(got, "dg").get("list") or [] if domain_ids else []:
        if str(item.get("name") or "").strip().lower() == domain:
            domain_id = str(item.get("id") or "")
            break
    existing: dict[str, str] = {}
    if account_ids:
        for item in _method_result(got, "ag").get("list") or []:
            email = str(item.get("emailAddress") or "").strip().casefold()
            account_id = str(item.get("id") or "")
            if email and account_id:
                existing[email] = account_id
    if not domain_id:
        raise RuntimeError(f"Stalwart has no domain {domain!r}, so mail-users mailboxes cannot be created")
    root = Path(__file__).resolve().parent.parent
    tracked_file = state_path(root)
    tracked = load_tracked(tracked_file)
    reserved = {recovery_admin_user(config).strip().casefold()}
    to_create, to_delete, next_tracked = plan_mailboxes(
        desired, existing, tracked, reserved=reserved
    )
    created = 0
    for email, part in sorted(to_create.items()):
        result = _method_result(
            jmap(
                [
                    [
                        "x:Account/set",
                        {"create": {email: account_create_payload(part, domain_id)}},
                        "c",
                    ]
                ]
            ),
            "c",
        )
        if result.get("notCreated"):
            raise RuntimeError(f"Could not create mailbox {email}: {result['notCreated']}")
        created += 1
    removed = 0
    if to_delete:
        result = _method_result(
            jmap([["x:Account/set", {"destroy": list(to_delete.values())}, "d"]]),
            "d",
        )
        if result.get("notDestroyed"):
            raise RuntimeError(f"Could not remove mailboxes: {result['notDestroyed']}")
        removed = len(to_delete)
    save_tracked(tracked_file, next_tracked)
    message = f"Mailboxes: {created} created, {removed} removed, {len(desired)} in mail-users"
    print(f"  {message}")
    return message
