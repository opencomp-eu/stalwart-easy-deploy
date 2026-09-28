"""Tests for Kanidm contact sync and calendar/address-book names."""

from __future__ import annotations

import json
import os

import pytest

from scripts.groupware import (
    ADDRESS_BOOK_NAME,
    CALENDAR_NAME,
    KANIDM_UID_PREFIX,
    Person,
    apply_default_names,
    contacts_sync_enabled,
    desired_collection_name,
    impersonation_username,
    mailbox_login,
    parse_kanidm_people,
    plan_contact_changes,
    reconcile_contacts_timer,
    render_sync_timer,
    sync_mailbox,
)


ALICE = Person(
    uuid="alice-uuid",
    name="alice",
    display_name="Alice",
    emails=("alice@opencomp.eu",),
)
BOB = Person(
    uuid="bob-uuid",
    name="bob",
    display_name="Bob",
    emails=("bob@opencomp.eu",),
)


def test_contacts_sync_follows_kanidm_and_can_be_disabled():
    assert contacts_sync_enabled({"identity": {"provider": "kanidm"}}) is True
    assert contacts_sync_enabled({"identity": {"provider": "internal"}}) is False
    assert contacts_sync_enabled({"identity": {"provider": "kanidm", "managed": False}}) is False
    assert contacts_sync_enabled({"identity": {"provider": "kanidm", "contacts": False}}) is False
    assert (
        contacts_sync_enabled({"identity": {"provider": "kanidm", "contacts": {"enabled": False}}})
        is False
    )


def test_parse_kanidm_people_keeps_mail_and_ignores_the_rest():
    raw = (
        "warning: cached token refreshed\n"
        + json.dumps(
            [
                {
                    "name": ["alice"],
                    "displayname": ["Alice Example"],
                    "mail": ["Alice@opencomp.eu", "alice@opencomp.eu"],
                    "uuid": ["u-alice"],
                    "class": ["person", "account"],
                },
                {"name": ["carol"], "uuid": ["u-carol"], "class": ["person"]},
                {"name": ["svc"], "uuid": ["u-svc"], "class": ["service_account"]},
            ]
        )
    )
    people = parse_kanidm_people(raw)
    assert [person.uuid for person in people] == ["u-alice"]
    assert people[0].display_name == "Alice Example"
    assert people[0].emails == ("alice@opencomp.eu",)
    assert people[0].uid == f"{KANIDM_UID_PREFIX}u-alice"


def test_stock_names_become_address_book_and_calendar():
    labels = {"bob@opencomp.eu", "bob"}
    assert (
        desired_collection_name(
            "Stalwart Address Book (bob@opencomp.eu)",
            stalwart_base="Stalwart Address Book",
            plain_base=ADDRESS_BOOK_NAME,
            labels=labels,
        )
        == ADDRESS_BOOK_NAME
    )
    assert (
        desired_collection_name(
            "Address Book (bob)",
            stalwart_base="Stalwart Address Book",
            plain_base=ADDRESS_BOOK_NAME,
            labels=labels,
        )
        == ADDRESS_BOOK_NAME
    )
    assert (
        desired_collection_name(
            "Address Book",
            stalwart_base="Stalwart Address Book",
            plain_base=ADDRESS_BOOK_NAME,
            labels=labels,
        )
        is None
    )
    assert (
        desired_collection_name(
            "Family",
            stalwart_base="Stalwart Address Book",
            plain_base=ADDRESS_BOOK_NAME,
            labels=labels,
        )
        is None
    )
    assert (
        desired_collection_name(
            "Stalwart Calendar (bob@opencomp.eu)",
            stalwart_base="Stalwart Calendar",
            plain_base=CALENDAR_NAME,
            labels=labels,
        )
        == CALENDAR_NAME
    )


def test_plan_adds_colleagues_updates_names_and_drops_removed_people():
    existing = [
        {
            "id": "keep",
            "uid": "personal",
            "name": {"full": "Mum"},
            "emails": {"0": {"address": "mum@example.com"}},
        },
        {
            "id": "old-alice",
            "uid": ALICE.uid,
            "name": {"full": "Old Alice"},
            "emails": {"0": {"address": "alice@opencomp.eu"}},
        },
        {
            "id": "gone",
            "uid": f"{KANIDM_UID_PREFIX}carol",
            "name": {"full": "Carol"},
            "emails": {"0": {"address": "carol@opencomp.eu"}},
        },
    ]
    create, update, destroy = plan_contact_changes(
        [ALICE, BOB],
        existing,
        "book-1",
        {"bob@opencomp.eu"},
    )
    assert create == {}
    assert "bob-uuid" not in json.dumps(update)
    assert update["old-alice"]["name"]["full"] == "Alice"
    assert destroy == ["gone"]
    untouched = [card for card in existing if card["id"] == "keep"]
    assert untouched[0]["uid"] == "personal"


def test_impersonation_username_puts_the_mailbox_first():
    assert impersonation_username("bob@opencomp.eu", "admin") == "bob@opencomp.eu%admin"
    with pytest.raises(ValueError):
        impersonation_username("bob%extra", "admin")


def test_mailbox_login_uses_the_email_address():
    assert mailbox_login({"name": "bob", "emailAddress": "bob@opencomp.eu"}, "opencomp.eu") == (
        "bob@opencomp.eu"
    )
    assert mailbox_login({"name": "bob"}, "opencomp.eu") == "bob@opencomp.eu"


class _FakeJmap:
    def __init__(self):
        self.books = [{"id": "b1", "name": "Stalwart Address Book (bob@opencomp.eu)"}]
        self.calendars = [{"id": "c1", "name": "Stalwart Calendar (bob@opencomp.eu)"}]
        self.cards = [
            {
                "id": "old-alice",
                "uid": ALICE.uid,
                "name": {"full": "Old Alice"},
                "emails": {"0": {"address": "alice@opencomp.eu"}},
            }
        ]

    def __call__(self, calls):
        method, args, call_id = calls[0]
        if method.endswith("/query"):
            if method.startswith("AddressBook"):
                ids = [item["id"] for item in self.books]
            elif method.startswith("Calendar"):
                ids = [item["id"] for item in self.calendars]
            else:
                ids = [item["id"] for item in self.cards]
            return {"methodResponses": [[method, {"ids": ids, "total": len(ids)}, call_id]]}
        if method.endswith("/get"):
            wanted = set(args.get("ids") or [])
            if method.startswith("AddressBook"):
                pool = self.books
            elif method.startswith("Calendar"):
                pool = self.calendars
            else:
                pool = self.cards
            return {
                "methodResponses": [
                    [method, {"list": [item for item in pool if item["id"] in wanted]}, call_id]
                ]
            }
        if method.endswith("/set"):
            created = {}
            updated = {}
            destroyed = []
            pool = self.books if method.startswith("AddressBook") else (
                self.calendars if method.startswith("Calendar") else self.cards
            )
            for key, value in (args.get("create") or {}).items():
                new_id = f"new-{key}"
                pool.append({"id": new_id, **value})
                created[key] = {"id": new_id}
            for item_id, patch in (args.get("update") or {}).items():
                for item in pool:
                    if item["id"] == item_id:
                        item.update(patch)
                        updated[item_id] = None
            for item_id in args.get("destroy") or []:
                pool[:] = [item for item in pool if item["id"] != item_id]
                destroyed.append(item_id)
            return {
                "methodResponses": [
                    [
                        method,
                        {"created": created, "updated": updated, "destroyed": destroyed},
                        call_id,
                    ]
                ]
            }
        raise AssertionError(method)


def test_sync_mailbox_renames_collections_and_upserts_kanidm_people():
    jmap = _FakeJmap()
    stats = sync_mailbox(
        jmap,
        account_id="acct-bob",
        account_name="bob@opencomp.eu",
        domain="opencomp.eu",
        people=[ALICE, BOB],
    )
    assert stats.address_book_renamed is True
    assert stats.calendar_renamed is True
    assert stats.updated == 1
    assert stats.created == 0
    assert jmap.books[0]["name"] == "Address Book"
    assert jmap.calendars[0]["name"] == "Calendar"
    assert jmap.cards[0]["name"]["full"] == "Alice"
    assert all(card.get("uid") != BOB.uid for card in jmap.cards)


def test_sync_mailbox_removes_people_deleted_from_kanidm():
    jmap = _FakeJmap()
    sync_mailbox(
        jmap,
        account_id="acct-bob",
        account_name="bob@opencomp.eu",
        domain="opencomp.eu",
        people=[BOB],
    )
    assert jmap.cards == []


def test_default_names_are_written_on_the_singletons():
    calls = []

    def jmap(method_calls):
        calls.extend(method_calls)
        return {
            "methodResponses": [
                [method_calls[0][0], {"updated": {"singleton": None}}, method_calls[0][2]]
            ]
        }

    apply_default_names(jmap)
    updates = [call for call in calls if call[0] in {"x:AddressBook/set", "x:Calendar/set"}]
    assert updates[0][1]["update"]["singleton"]["defaultDisplayName"] == "Address Book"
    assert updates[1][1]["update"]["singleton"]["defaultDisplayName"] == "Calendar"


def test_contact_timer_installs_and_removes(tmp_path, monkeypatch):
    monkeypatch.setenv("EASYDEPLOY_ASSUME_SYSTEMD", "1")
    runs = []

    def run_command(command, check):
        runs.append(command)
        return None

    config = {"identity": {"provider": "kanidm"}}
    message = reconcile_contacts_timer(
        config,
        project_root=tmp_path,
        unit_dir=tmp_path,
        run_command=run_command,
    )
    assert message == "Kanidm contact sync timer installed or updated."
    timer = (tmp_path / "stalwart-easy-deploy-contacts.timer").read_text()
    assert "OnCalendar=*:0/5" in timer
    assert render_sync_timer("stalwart-easy-deploy-contacts") == timer
    assert ["systemctl", "enable", "--now", "stalwart-easy-deploy-contacts.timer"] in runs

    message = reconcile_contacts_timer(
        {"identity": {"provider": "kanidm", "contacts": False}},
        project_root=tmp_path,
        unit_dir=tmp_path,
        run_command=run_command,
    )
    assert message == "Kanidm contact sync timer removed."
    assert not (tmp_path / "stalwart-easy-deploy-contacts.timer").exists()
    assert os.environ["EASYDEPLOY_ASSUME_SYSTEMD"] == "1"
